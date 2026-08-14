"""Authorization-aware idempotency preflight and replay hydration.

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

Every statement is schema-qualified through the Task 6 Core metadata and
parameter-bound; driver failures are routed through
:mod:`postgresql_source_store.error_mapping` so SQLSTATE, SQL, parameters and
driver text never leave the adapter. The commit methods land in later tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4

import sqlalchemy as sa
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
from personal_os.sources.fingerprint import RequestFingerprint, SourceVersionCommand
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import DatabaseRetryPolicy
from postgresql_source_store.tables import (
    audit_events,
    content_objects,
    devices,
    source_versions,
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

#: Workspace and device lifecycle states accepted by revalidation.
_WORKSPACE_STATUS_ACTIVE: Final[str] = "active"
_DEVICE_STATUS_ACTIVE: Final[str] = "active"

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
        """Build the typed row from one positional result row of the lookup."""
        return cls(
            workspace_id=row[0],
            source_id=row[1],
            event_id=row[2],
            event_sequence=row[3],
            event_type=row[4],
            base_version_id=row[5],
            committed_version_id=row[6],
            idempotency_key=row[7],
            request_fingerprint=row[8],
            committed_at=row[9],
            source_version_id=row[10],
            content_version=row[11],
            content_hash=row[12],
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


class PostgresqlSourcePublicationStore:
    """Durable source-publication store over the canonical PostgreSQL baseline.

    The store takes the composition-owned :class:`AsyncEngine`; it opens no
    connection at construction. ``resolve_committed`` runs the lock-free
    preflight inside one ``READ COMMITTED`` transaction with the pinned ``SET
    LOCAL`` bounds and the bounded contention retry; the commit methods arrive
    with the later create/update tasks.
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
        raise NotImplementedError("commit_create lands with the create transaction task")

    async def commit_update(
        self,
        command: UpdateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        raise NotImplementedError("commit_update lands with the update transaction task")

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
