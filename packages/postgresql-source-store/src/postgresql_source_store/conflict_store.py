"""Atomic conflict capture, replay and resolution over the canonical baseline.

:class:`PostgresqlSourceConflictStore` implements the durable
:class:`~personal_os.source_conflicts.ports.SourceConflictStore` port over the
``20260902_01`` migrated aggregate. ``capture`` runs one ``READ COMMITTED``
transaction behind the pinned ``SET LOCAL`` bounds: the conflict idempotency
advisory lock first, the trusted workspace/device revalidation, the replay and
identity-mismatch recheck, then — when the conflict binds a source — the
source advisory lock (the same consistency boundary a competing publication
or lifecycle mutation takes) before the accepted ``conflict_capture`` sync
event, the immutable evidence row and the audit row commit together; the
source current pointer is never touched. A locator collision that has not
identified a canonical source cannot bind the ``NOT NULL`` sync-event source,
so its evidence identity lives entirely on the conflict row.

``resolve`` follows the same prefix with the resolution idempotency lock,
replays by the resolution event identity, then locks the conflict row
``FOR UPDATE`` and — for a sourced conflict — the source advisory lock and the
current-pointer row, rechecks the reviewed remote version against the current
canonical state, and either commits the winner (``keep_remote`` closes with no
version; ``keep_local``/``save_merged`` publish exactly one immutable source
version against the reviewed remote with two projection intents and the
``conflict_resolve`` event) or records the stale attempt by superseding the
predecessor and inserting the open successor bound to the newer observed
remote, retaining the original candidate and evidence untouched. Terminal
states are replayed, never duplicated; a fresh event against a terminal
conflict is the typed state-invalid rejection.

Every statement is schema-qualified through the Task 6 Core metadata and
parameter-bound; driver failures are classified through the shared SQLSTATE
classifier and mapped onto the closed ``source_conflict_*`` registry so
SQLSTATE, SQL, parameters and driver text never leave the adapter.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Final, cast
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.source_conflicts.commands import (
    CaptureConflictCommand,
    ConflictResolutionResult,
    ResolveConflictCommand,
)
from personal_os.source_conflicts.contracts import (
    TERMINAL_CONFLICT_STATUSES,
    VERSION_PUBLISHING_RESOLUTIONS,
    ConflictCandidate,
    ConflictCandidateKind,
    ConflictIdempotencyKey,
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
    ConflictStatus,
    SourceConflict,
)
from personal_os.source_conflicts.errors import (
    BASE_VERSION_INVALID,
    CANDIDATE_INVALID,
    CANDIDATE_OBJECT_INVALID,
    DEVICE_ID_INVALID,
    REMOTE_VERSION_INVALID,
    SOURCE_ID_INVALID,
    WORKSPACE_ID_INVALID,
    SourceConflictError,
)
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import (
    RETRY_JITTER_MAXIMUM_SECONDS,
    RETRY_JITTER_MINIMUM_SECONDS,
    DatabaseFailureKind,
    classify_database_failure,
)
from postgresql_source_store.locks import (
    conflict_idempotency_lock_statement,
    source_lock_statement,
)
from postgresql_source_store.tables import (
    audit_events,
    content_objects,
    devices,
    projection_intents,
    source_conflicts,
    source_versions,
    sources,
    sync_events,
    workspaces,
)

#: Accepted ``sync_events.event_type`` literals of the conflict store.
CAPTURE_EVENT_TYPE: Final[str] = "conflict_capture"
RESOLUTION_EVENT_TYPE: Final[str] = "conflict_resolve"

#: Audit constants of the in-transaction conflict transitions.
CONFLICT_AUDIT_TARGET_KIND: Final[str] = "source_conflict"
CAPTURE_AUDIT_ACTION: Final[str] = "source.conflict_captured"
RESOLUTION_AUDIT_ACTION: Final[str] = "source.conflict_resolved"
SUPERSEDED_AUDIT_ACTION: Final[str] = "source.conflict_superseded"
AUDIT_RESULT_SUCCEEDED: Final[str] = "succeeded"
AUDIT_ACTOR_KIND_DEVICE: Final[str] = "device"

#: Canonical transition literals shared with the publication store.
PROJECTION_KIND_QDRANT: Final[str] = "qdrant"
PROJECTION_KIND_NEO4J: Final[str] = "neo4j"
PROJECTION_OPERATION_UPSERT: Final[str] = "upsert"

#: The resulting version of a conflict resolution is authored by the device
#: whose retained candidate or inbox evidence the explicit user choice acted
#: on; the resolve command carries no actor of its own.
_AUTHOR_KIND_DEVICE: Final[str] = "device"

#: Lifecycle and source states referenced by the resolution transitions.
_WORKSPACE_STATUS_ACTIVE: Final[str] = "active"
_DEVICE_STATUS_ACTIVE: Final[str] = "active"
_SOURCE_STATE_ACTIVE: Final[str] = "active"
_SOURCE_STATE_STORED_NOT_INDEXED: Final[str] = "stored_not_indexed"

#: The only source states a publishing resolution may commit over.
_PUBLISHABLE_SOURCE_STATES: Final[frozenset[str]] = frozenset(
    {_SOURCE_STATE_ACTIVE, _SOURCE_STATE_STORED_NOT_INDEXED}
)

#: The closed bound of one open-conflict listing page.
MAX_OPEN_CONFLICT_PAGE: Final[int] = 200

_CONFLICT_COLUMNS: Final[tuple[Any, ...]] = (
    source_conflicts.c.conflict_id,
    source_conflicts.c.workspace_id,
    source_conflicts.c.source_id,
    source_conflicts.c.conflict_kind,
    source_conflicts.c.status,
    source_conflicts.c.originating_event_id,
    source_conflicts.c.originating_device_id,
    source_conflicts.c.capture_idempotency_key,
    source_conflicts.c.base_version_id,
    source_conflicts.c.observed_remote_version_id,
    source_conflicts.c.candidate_kind,
    source_conflicts.c.verified_candidate_object_id,
    source_conflicts.c.normalized_locator,
    source_conflicts.c.resolution_kind,
    source_conflicts.c.resolution_event_id,
    source_conflicts.c.resolution_idempotency_key,
    source_conflicts.c.resulting_version_id,
    source_conflicts.c.successor_conflict_id,
    source_conflicts.c.captured_at,
    source_conflicts.c.closed_at,
)

_CONFLICT_TOKEN_TO_KIND: Final[Mapping[str, ConflictKind]] = {
    kind.value: kind for kind in ConflictKind
}
_CONFLICT_TOKEN_TO_STATUS: Final[Mapping[str, ConflictStatus]] = {
    status.value: status for status in ConflictStatus
}
_CANDIDATE_TOKEN_TO_KIND: Final[Mapping[str, ConflictCandidateKind]] = {
    kind.value: kind for kind in ConflictCandidateKind
}
_RESOLUTION_TOKEN_TO_KIND: Final[Mapping[str, ConflictResolutionKind]] = {
    kind.value: kind for kind in ConflictResolutionKind
}

type _ConflictRowMapping = Mapping[str, Any]


# --- pure hydration and statement builders -----------------------------------


def hydrate_source_conflict(row: _ConflictRowMapping) -> SourceConflict:
    """Hydrate one stored conflict row into the frozen read model.

    Fail-closed: any token outside the closed vocabularies, any shape the
    domain contract forbids and any naive timestamp is the typed
    ``source_conflict_state_invalid`` rejection, never a partially hydrated
    value. Locators, keys and object references cross only as opaque column
    values into the frozen model.
    """
    conflict_kind = _CONFLICT_TOKEN_TO_KIND.get(str(row["conflict_kind"]))
    status = _CONFLICT_TOKEN_TO_STATUS.get(str(row["status"]))
    candidate_kind = _CANDIDATE_TOKEN_TO_KIND.get(str(row["candidate_kind"]))
    resolution_kind_value = row["resolution_kind"]
    resolution_kind = (
        _RESOLUTION_TOKEN_TO_KIND.get(str(resolution_kind_value))
        if resolution_kind_value is not None
        else None
    )
    if conflict_kind is None or status is None or candidate_kind is None:
        raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID)
    if resolution_kind_value is not None and resolution_kind is None:
        raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID)
    try:
        candidate = ConflictCandidate(
            candidate_kind=candidate_kind,
            verified_candidate_object_id=row["verified_candidate_object_id"],
        )
        return SourceConflict(
            conflict_id=row["conflict_id"],
            workspace_id=row["workspace_id"],
            source_id=row["source_id"],
            conflict_kind=conflict_kind,
            status=status,
            originating_event_id=row["originating_event_id"],
            originating_device_id=row["originating_device_id"],
            base_version_id=row["base_version_id"],
            observed_remote_version_id=row["observed_remote_version_id"],
            candidate=candidate,
            captured_at=row["captured_at"],
            resolution_kind=resolution_kind,
            resolution_event_id=row["resolution_event_id"],
            resulting_version_id=row["resulting_version_id"],
            successor_conflict_id=row["successor_conflict_id"],
            closed_at=row["closed_at"],
        )
    except AttributeError, TypeError, ValueError:
        raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID) from None


def capture_replay_by_key_statement(
    workspace_id: UUID, idempotency_key: ConflictIdempotencyKey
) -> sa.Select[tuple[Any, ...]]:
    """Build the schema-qualified, parameter-bound capture replay lookup."""
    return sa.select(*_CONFLICT_COLUMNS).where(
        source_conflicts.c.workspace_id == workspace_id,
        source_conflicts.c.capture_idempotency_key == idempotency_key.value,
    )


def captured_conflict_by_event_statement(
    originating_event_id: UUID, workspace_id: UUID
) -> sa.Select[tuple[Any, ...]]:
    """Build the schema-qualified, parameter-bound originating-event lookup."""
    return sa.select(*_CONFLICT_COLUMNS).where(
        source_conflicts.c.workspace_id == workspace_id,
        source_conflicts.c.originating_event_id == originating_event_id,
    )


def conflict_read_statement(
    conflict_id: UUID,
    workspace_id: UUID,
    *,
    for_update: bool = False,
) -> sa.Select[tuple[Any, ...]]:
    """Build the workspace-scoped conflict read, optionally row-locked."""
    statement = sa.select(*_CONFLICT_COLUMNS).where(
        source_conflicts.c.conflict_id == conflict_id,
        source_conflicts.c.workspace_id == workspace_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return statement


def list_open_conflicts_statement(
    workspace_id: UUID,
    *,
    limit: int,
    exclusive_start_conflict_id: UUID | None,
) -> sa.Select[tuple[Any, ...]]:
    """Build the open-conflict page in stable conflict-identity order."""
    if not 1 <= limit <= MAX_OPEN_CONFLICT_PAGE:
        raise ValueError(f"limit must be between 1 and {MAX_OPEN_CONFLICT_PAGE} conflicts")
    statement = (
        sa.select(*_CONFLICT_COLUMNS)
        .where(
            source_conflicts.c.workspace_id == workspace_id,
            source_conflicts.c.status == ConflictStatus.OPEN.value,
        )
        .order_by(source_conflicts.c.conflict_id.asc())
        .limit(sa.bindparam("open_conflict_page_limit", limit))
    )
    if exclusive_start_conflict_id is not None:
        statement = statement.where(source_conflicts.c.conflict_id > exclusive_start_conflict_id)
    return statement


def _resolution_replay_by_event_statement(
    workspace_id: UUID, resolution_event_id: UUID
) -> sa.Select[tuple[Any, ...]]:
    return sa.select(*_CONFLICT_COLUMNS).where(
        source_conflicts.c.workspace_id == workspace_id,
        source_conflicts.c.resolution_event_id == resolution_event_id,
    )


def _resolution_key_reuse_statement(
    workspace_id: UUID, idempotency_key: ConflictIdempotencyKey
) -> sa.Select[tuple[Any, ...]]:
    return sa.select(*_CONFLICT_COLUMNS).where(
        source_conflicts.c.workspace_id == workspace_id,
        source_conflicts.c.resolution_idempotency_key == idempotency_key.value,
    )


def _capture_key_reuse_statement(
    workspace_id: UUID, idempotency_key: ConflictIdempotencyKey
) -> sa.Select[tuple[Any, ...]]:
    return sa.select(*_CONFLICT_COLUMNS).where(
        source_conflicts.c.workspace_id == workspace_id,
        source_conflicts.c.capture_idempotency_key == idempotency_key.value,
    )


def compute_capture_request_fingerprint(command: CaptureConflictCommand) -> str:
    """Compute the deterministic sha256 sync-event fingerprint of a capture.

    The material is the command's opaque identity and evidence only — closed
    labels, UUIDs and the digest of the locator snapshot — so an exact replay
    recomputes the identical value while no path, digest or key ever renders
    outside the hashed column.
    """
    material = "|".join(
        (
            "source_conflict_capture/v1",
            str(command.workspace_id),
            str(command.source_id),
            command.conflict_kind.value,
            str(command.originating_event_id),
            str(command.originating_device_id),
            str(command.base_version_id),
            str(command.observed_remote_version_id),
            command.candidate.candidate_kind.value,
            str(command.candidate.verified_candidate_object_id),
            sha256(command.normalized_locator.value.encode("utf-8")).hexdigest()
            if command.normalized_locator is not None
            else "-",
        )
    )
    return sha256(material.encode("utf-8")).hexdigest()


def compute_resolution_request_fingerprint(command: ResolveConflictCommand) -> str:
    """Compute the deterministic sha256 sync-event fingerprint of a resolution."""
    material = "|".join(
        (
            "source_conflict_resolution/v1",
            str(command.conflict_id),
            str(command.reviewed_remote_version_id),
            command.resolution_kind.value,
            str(command.resolution_event_id),
            str(command.verified_candidate_object_id),
        )
    )
    return sha256(material.encode("utf-8")).hexdigest()


# --- database failure boundary -------------------------------------------------


def map_source_conflict_database_failure(cause: BaseException) -> ApplicationError:
    """Map a driver failure onto the closed conflict error registry.

    Contention maps to the retryable dependency outage; an integrity violation
    or non-database failure maps to the redacted internal error; unavailability
    and any unclassified database failure map to the retryable commit-outcome-
    unknown because the transaction outcome could not be determined. The cause
    stays chained only.
    """
    failure_kind = classify_database_failure(cause)
    if failure_kind is DatabaseFailureKind.CONTENTION:
        return SourceConflictError(ErrorCode.SOURCE_CONFLICT_DEPENDENCY_UNAVAILABLE)
    if failure_kind is DatabaseFailureKind.INTEGRITY:
        return InternalApplicationError(ErrorCode.INTERNAL_ERROR)
    if failure_kind is DatabaseFailureKind.NOT_DATABASE:
        return InternalApplicationError(ErrorCode.INTERNAL_ERROR)
    return SourceConflictError(ErrorCode.SOURCE_CONFLICT_COMMIT_OUTCOME_UNKNOWN)


@dataclass(frozen=True, slots=True)
class SourceConflictDatabaseRetryPolicy:
    """Bounded retry for the conflict store over the shared SQLSTATE classifier.

    At most ``maximum_attempts`` attempts run with the shared cancellable
    50-250 ms jitter. Typed application errors pass through untouched. Because
    capture and resolve replay by event identity under the idempotency
    advisory lock, a connection-class failure with an uncertain commit outcome
    is safely retried: the next attempt's replay lookup returns the stored
    outcome when the transaction committed and re-executes it only after
    proving absence.
    """

    maximum_attempts: int = 3

    async def run[T](
        self,
        operation: Callable[[int], Awaitable[T]],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> T:
        for attempt in range(1, self.maximum_attempts + 1):
            try:
                return await operation(attempt)
            except ApplicationError:
                raise
            except Exception as cause:
                failure_kind = classify_database_failure(cause)
                if failure_kind is DatabaseFailureKind.NOT_DATABASE:
                    raise map_source_conflict_database_failure(cause) from cause
                if failure_kind is DatabaseFailureKind.INTEGRITY:
                    raise map_source_conflict_database_failure(cause) from cause
                if attempt < self.maximum_attempts:
                    await sleep(jitter(RETRY_JITTER_MINIMUM_SECONDS, RETRY_JITTER_MAXIMUM_SECONDS))
                    continue
                raise map_source_conflict_database_failure(cause) from cause
        raise AssertionError("retry loop exhausted without a result")


# --- backend identities ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConflictCaptureIdentities:
    """Backend UUIDv7 identities for one capture service invocation."""

    conflict_id: UUID
    audit_event_id: UUID

    @classmethod
    def allocate(cls) -> ConflictCaptureIdentities:
        """Allocate the two fresh time-ordered UUIDv7 identities."""
        return cls(conflict_id=uuid7(), audit_event_id=uuid7())


@dataclass(frozen=True, slots=True)
class ConflictResolutionIdentities:
    """Backend UUIDv7 identities for one resolution service invocation."""

    successor_conflict_id: UUID
    resulting_version_id: UUID
    qdrant_intent_id: UUID
    neo4j_intent_id: UUID
    audit_event_id: UUID

    @classmethod
    def allocate(cls) -> ConflictResolutionIdentities:
        """Allocate the five fresh time-ordered UUIDv7 identities."""
        return cls(
            successor_conflict_id=uuid7(),
            resulting_version_id=uuid7(),
            qdrant_intent_id=uuid7(),
            neo4j_intent_id=uuid7(),
            audit_event_id=uuid7(),
        )


@dataclass(frozen=True, slots=True)
class _LockedSourceState:
    """The locked ``sources`` row of the resolution recheck."""

    sync_state: str | None
    current_version_id: UUID | None
    deleted_at: datetime | None


def _default_clock() -> datetime:
    return datetime.now(UTC)


# --- the store ------------------------------------------------------------------


class PostgresqlSourceConflictStore:
    """Durable source-conflict store over the canonical PostgreSQL baseline.

    The store takes the composition-owned :class:`AsyncEngine` and the
    injectable aware-UTC clock that owns every transition timestamp
    (``captured_at``, ``closed_at``, the successor's capture time); it opens no
    connection at construction. Every mutation runs inside one ``READ
    COMMITTED`` transaction behind the pinned ``SET LOCAL`` bounds and the
    bounded contention retry, with the idempotency advisory lock acquired
    before any conflict row or source lock.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        clock: Callable[[], datetime] | None = None,
        *,
        retry: SourceConflictDatabaseRetryPolicy | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock if clock is not None else _default_clock
        self._retry = retry if retry is not None else SourceConflictDatabaseRetryPolicy()

    # --- capture ----------------------------------------------------------------

    async def capture(
        self,
        command: CaptureConflictCommand,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict:
        identities = ConflictCaptureIdentities.allocate()
        return await self._retry.run(
            lambda _attempt: self._capture_once(command, diagnostic_context, identities)
        )

    async def _capture_once(
        self,
        command: CaptureConflictCommand,
        diagnostic_context: DiagnosticContext,
        identities: ConflictCaptureIdentities,
    ) -> SourceConflict:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            await connection.execute(
                conflict_idempotency_lock_statement(command.workspace_id, command.idempotency_key)
            )
            replay_row = await self._fetch_conflict_row(
                connection,
                capture_replay_by_key_statement(command.workspace_id, command.idempotency_key),
            )
            if replay_row is not None:
                if replay_row["originating_event_id"] != command.originating_event_id or (
                    not self._capture_evidence_matches(replay_row, command)
                ):
                    raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH)
                return hydrate_source_conflict(replay_row)
            event_bound_row = await self._fetch_conflict_row(
                connection,
                captured_conflict_by_event_statement(
                    command.originating_event_id, command.workspace_id
                ),
            )
            if event_bound_row is not None:
                # The key lookup already missed, so this event identity is
                # held under another idempotency key.
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH)
            if await self._sync_event_key_held_by_another_event(
                connection,
                command.workspace_id,
                command.idempotency_key,
                command.originating_event_id,
            ):
                # The workspace key is already bound to an accepted sync event
                # outside this conflict (or an orphaned event row); the unique
                # constraint would be the final arbiter, but the typed
                # mismatch is the closed reason the caller must see.
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH)
            await self._validate_capture_references(connection, command)
            if command.source_id is not None:
                await connection.execute(source_lock_statement(command.source_id))
            if command.source_id is not None:
                await self._insert_conflict_event(
                    connection,
                    command,
                    compute_capture_request_fingerprint(command),
                )
            captured_at = self._clock()
            await self._insert_conflict_row(
                connection, command, identities.conflict_id, captured_at
            )
            await self._insert_conflict_audit(
                connection,
                workspace_id=command.workspace_id,
                device_id=command.originating_device_id,
                action=CAPTURE_AUDIT_ACTION,
                target_conflict_id=identities.conflict_id,
                diagnostic_context=diagnostic_context,
                audit_event_id=identities.audit_event_id,
            )
            return SourceConflict(
                conflict_id=identities.conflict_id,
                workspace_id=command.workspace_id,
                source_id=command.source_id,
                conflict_kind=command.conflict_kind,
                status=ConflictStatus.OPEN,
                originating_event_id=command.originating_event_id,
                originating_device_id=command.originating_device_id,
                base_version_id=command.base_version_id,
                observed_remote_version_id=command.observed_remote_version_id,
                candidate=command.candidate,
                captured_at=captured_at,
                resolution_kind=None,
                resolution_event_id=None,
                resulting_version_id=None,
                successor_conflict_id=None,
                closed_at=None,
            )

    @staticmethod
    def _capture_evidence_matches(
        row: _ConflictRowMapping, command: CaptureConflictCommand
    ) -> bool:
        """Compare the stored evidence of a replay candidate exactly."""
        stored_locator = row["normalized_locator"]
        return bool(
            row["workspace_id"] == command.workspace_id
            and row["source_id"] == command.source_id
            and row["conflict_kind"] == command.conflict_kind.value
            and row["originating_device_id"] == command.originating_device_id
            and row["base_version_id"] == command.base_version_id
            and row["observed_remote_version_id"] == command.observed_remote_version_id
            and row["candidate_kind"] == command.candidate.candidate_kind.value
            and row["verified_candidate_object_id"]
            == command.candidate.verified_candidate_object_id
            and stored_locator
            == (command.normalized_locator.value if command.normalized_locator else None)
        )

    async def _validate_capture_references(
        self, connection: AsyncConnection, command: CaptureConflictCommand
    ) -> None:
        """Revalidate every FK target so a bad reference rejects typed, not raw."""
        workspace_result = await connection.execute(
            sa.select(workspaces.c.status).where(workspaces.c.workspace_id == command.workspace_id)
        )
        if workspace_result.scalar_one_or_none() != _WORKSPACE_STATUS_ACTIVE:
            raise SourceConflictError(
                ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
                safe_details={"reason": WORKSPACE_ID_INVALID},
            )
        device_result = await connection.execute(
            sa.select(devices.c.status).where(
                devices.c.workspace_id == command.workspace_id,
                devices.c.device_id == command.originating_device_id,
            )
        )
        if device_result.scalar_one_or_none() != _DEVICE_STATUS_ACTIVE:
            raise SourceConflictError(
                ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
                safe_details={"reason": DEVICE_ID_INVALID},
            )
        if command.candidate.verified_candidate_object_id is not None:
            object_result = await connection.execute(
                sa.select(content_objects.c.content_object_id).where(
                    content_objects.c.content_object_id
                    == command.candidate.verified_candidate_object_id
                )
            )
            if object_result.scalar_one_or_none() is None:
                raise SourceConflictError(
                    ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
                    safe_details={"reason": CANDIDATE_OBJECT_INVALID},
                )
        if command.source_id is None:
            return
        source_result = await connection.execute(
            sa.select(sources.c.source_id).where(
                sources.c.source_id == command.source_id,
                sources.c.workspace_id == command.workspace_id,
            )
        )
        if source_result.scalar_one_or_none() is None:
            raise SourceConflictError(
                ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
                safe_details={"reason": SOURCE_ID_INVALID},
            )
        if command.base_version_id is not None and not await self._version_exists(
            connection, command.workspace_id, command.source_id, command.base_version_id
        ):
            raise SourceConflictError(
                ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
                safe_details={"reason": BASE_VERSION_INVALID},
            )
        if command.observed_remote_version_id is not None and not await self._version_exists(
            connection,
            command.workspace_id,
            command.source_id,
            command.observed_remote_version_id,
        ):
            raise SourceConflictError(
                ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
                safe_details={"reason": REMOTE_VERSION_INVALID},
            )

    @staticmethod
    async def _version_exists(
        connection: AsyncConnection, workspace_id: UUID, source_id: UUID, version_id: UUID
    ) -> bool:
        result = await connection.execute(
            sa.select(source_versions.c.source_version_id).where(
                source_versions.c.workspace_id == workspace_id,
                source_versions.c.source_id == source_id,
                source_versions.c.source_version_id == version_id,
            )
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def _sync_event_key_held_by_another_event(
        connection: AsyncConnection,
        workspace_id: UUID,
        idempotency_key: ConflictIdempotencyKey,
        event_id: UUID,
    ) -> bool:
        """Report whether the workspace key is already bound to a foreign event.

        Mirrors ``uq_sync_events__idempotency_key`` so a key reused across
        domains rejects with the typed mismatch instead of surfacing the raw
        integrity violation the unique constraint would raise.
        """
        result = await connection.execute(
            sa.select(sync_events.c.event_id).where(
                sync_events.c.workspace_id == workspace_id,
                sync_events.c.idempotency_key == idempotency_key.value,
            )
        )
        held_by = result.scalar_one_or_none()
        return held_by is not None and held_by != event_id

    @staticmethod
    async def _insert_conflict_event(
        connection: AsyncConnection,
        command: CaptureConflictCommand,
        request_fingerprint: str,
    ) -> None:
        """Insert the accepted ``conflict_capture`` sync event of one capture."""
        await connection.execute(
            sa.insert(sync_events).values(
                event_id=command.originating_event_id,
                workspace_id=command.workspace_id,
                source_id=command.source_id,
                device_id=command.originating_device_id,
                committed_version_id=None,
                base_version_id=command.base_version_id,
                idempotency_key=command.idempotency_key.value,
                request_fingerprint=request_fingerprint,
                event_type=CAPTURE_EVENT_TYPE,
            )
        )

    @staticmethod
    async def _insert_conflict_row(
        connection: AsyncConnection,
        command: CaptureConflictCommand,
        conflict_id: UUID,
        captured_at: datetime,
    ) -> None:
        await connection.execute(
            sa.insert(source_conflicts).values(
                conflict_id=conflict_id,
                workspace_id=command.workspace_id,
                source_id=command.source_id,
                conflict_kind=command.conflict_kind.value,
                status=ConflictStatus.OPEN.value,
                originating_event_id=command.originating_event_id,
                originating_device_id=command.originating_device_id,
                capture_idempotency_key=command.idempotency_key.value,
                base_version_id=command.base_version_id,
                observed_remote_version_id=command.observed_remote_version_id,
                candidate_kind=command.candidate.candidate_kind.value,
                verified_candidate_object_id=command.candidate.verified_candidate_object_id,
                normalized_locator=(
                    command.normalized_locator.value
                    if command.normalized_locator is not None
                    else None
                ),
                captured_at=captured_at,
            )
        )

    async def _insert_conflict_audit(
        self,
        connection: AsyncConnection,
        *,
        workspace_id: UUID,
        device_id: UUID,
        action: str,
        target_conflict_id: UUID,
        diagnostic_context: DiagnosticContext,
        audit_event_id: UUID,
    ) -> None:
        await connection.execute(
            sa.insert(audit_events).values(
                audit_event_id=audit_event_id,
                workspace_id=workspace_id,
                actor_kind=AUDIT_ACTOR_KIND_DEVICE,
                actor_id=device_id,
                actor_reference=None,
                action=action,
                target_kind=CONFLICT_AUDIT_TARGET_KIND,
                target_id=target_conflict_id,
                request_id=diagnostic_context.request_id,
                client_request_id=diagnostic_context.client_request_id,
                trace_id=diagnostic_context.trace.trace_id.value,
                result=AUDIT_RESULT_SUCCEEDED,
                reason_code=None,
                safe_diff_hash=None,
            )
        )

    # --- reads -------------------------------------------------------------------

    async def find_captured_conflict(
        self,
        originating_event_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict | None:
        del diagnostic_context
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            row = await self._fetch_conflict_row(
                connection,
                captured_conflict_by_event_statement(originating_event_id, workspace_id),
            )
            return None if row is None else hydrate_source_conflict(row)

    async def list_open(
        self,
        workspace_id: UUID,
        *,
        limit: int,
        exclusive_start_conflict_id: UUID | None,
        diagnostic_context: DiagnosticContext,
    ) -> tuple[SourceConflict, ...]:
        del diagnostic_context
        statement = list_open_conflicts_statement(
            workspace_id, limit=limit, exclusive_start_conflict_id=exclusive_start_conflict_id
        )
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            rows = (await connection.execute(statement)).mappings().all()
            return tuple(hydrate_source_conflict(row) for row in rows)

    async def read(
        self,
        conflict_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict:
        return await self._read_conflict(
            conflict_id, workspace_id, diagnostic_context, for_update=False
        )

    async def read_for_resolution(
        self,
        conflict_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict:
        return await self._read_conflict(
            conflict_id, workspace_id, diagnostic_context, for_update=True
        )

    async def _read_conflict(
        self,
        conflict_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
        *,
        for_update: bool,
    ) -> SourceConflict:
        del diagnostic_context
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            row = await self._fetch_conflict_row(
                connection,
                conflict_read_statement(conflict_id, workspace_id, for_update=for_update),
            )
            if row is None:
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_NOT_FOUND)
            return hydrate_source_conflict(row)

    # --- resolution ----------------------------------------------------------------

    async def resolve(
        self,
        command: ResolveConflictCommand,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> ConflictResolutionResult:
        identities = ConflictResolutionIdentities.allocate()
        return await self._retry.run(
            lambda _attempt: self._resolve_once(
                command, workspace_id, diagnostic_context, identities
            )
        )

    async def _resolve_once(
        self,
        command: ResolveConflictCommand,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
        identities: ConflictResolutionIdentities,
    ) -> ConflictResolutionResult:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            await connection.execute(
                conflict_idempotency_lock_statement(workspace_id, command.idempotency_key)
            )
            replay_row = await self._fetch_conflict_row(
                connection,
                _resolution_replay_by_event_statement(workspace_id, command.resolution_event_id),
            )
            if replay_row is not None:
                if replay_row["resolution_idempotency_key"] != command.idempotency_key.value:
                    raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH)
                return await self._resolution_replay_result(connection, replay_row)
            key_reuse_row = await self._fetch_conflict_row(
                connection, _resolution_key_reuse_statement(workspace_id, command.idempotency_key)
            )
            if key_reuse_row is not None:
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH)
            capture_key_row = await self._fetch_conflict_row(
                connection, _capture_key_reuse_statement(workspace_id, command.idempotency_key)
            )
            if capture_key_row is not None:
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH)
            if await self._sync_event_key_held_by_another_event(
                connection, workspace_id, command.idempotency_key, command.resolution_event_id
            ):
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH)
            workspace_result = await connection.execute(
                sa.select(workspaces.c.status).where(workspaces.c.workspace_id == workspace_id)
            )
            if workspace_result.scalar_one_or_none() != _WORKSPACE_STATUS_ACTIVE:
                raise SourceConflictError(
                    ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
                    safe_details={"reason": WORKSPACE_ID_INVALID},
                )
            conflict_row = await self._fetch_conflict_row(
                connection,
                conflict_read_statement(command.conflict_id, workspace_id, for_update=True),
            )
            if conflict_row is None:
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_NOT_FOUND)
            conflict = hydrate_source_conflict(conflict_row)
            if conflict.status in TERMINAL_CONFLICT_STATUSES:
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID)
            if conflict.status is not ConflictStatus.OPEN:
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID)
            source_state = await self._select_locked_source_state(
                connection, workspace_id, conflict.source_id
            )
            now = self._clock()
            if command.reviewed_remote_version_id != source_state.current_version_id:
                return await self._stale_successor_transition(
                    connection,
                    command,
                    workspace_id,
                    conflict,
                    conflict_row,
                    source_state,
                    identities,
                    diagnostic_context,
                    now,
                )
            return await self._winner_transition(
                connection,
                command,
                workspace_id,
                conflict,
                conflict_row,
                source_state,
                identities,
                diagnostic_context,
                now,
            )

    async def _resolution_replay_result(
        self, connection: AsyncConnection, row: _ConflictRowMapping
    ) -> ConflictResolutionResult:
        """Return the frozen stored outcome of an exact resolution replay."""
        if row["status"] == ConflictStatus.RESOLVED.value:
            return ConflictResolutionResult(
                kind=ConflictResolutionOutcome.RESOLVED,
                conflict_id=row["conflict_id"],
                resolution_event_id=row["resolution_event_id"],
                resolution_kind=_require_resolution_kind(row["resolution_kind"]),
                resulting_version_id=row["resulting_version_id"],
                successor=None,
                completed_at=row["closed_at"],
            )
        if row["status"] == ConflictStatus.SUPERSEDED.value:
            successor_row = await self._fetch_conflict_row(
                connection,
                conflict_read_statement(row["successor_conflict_id"], row["workspace_id"]),
            )
            if successor_row is None:
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID)
            return ConflictResolutionResult(
                kind=ConflictResolutionOutcome.STALE_SUCCESSOR,
                conflict_id=row["conflict_id"],
                resolution_event_id=row["resolution_event_id"],
                resolution_kind=_require_resolution_kind(row["resolution_kind"]),
                resulting_version_id=None,
                successor=hydrate_source_conflict(successor_row),
                completed_at=row["closed_at"],
            )
        raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID)

    async def _stale_successor_transition(
        self,
        connection: AsyncConnection,
        command: ResolveConflictCommand,
        workspace_id: UUID,
        conflict: SourceConflict,
        conflict_row: _ConflictRowMapping,
        source_state: _LockedSourceState,
        identities: ConflictResolutionIdentities,
        diagnostic_context: DiagnosticContext,
        now: datetime,
    ) -> ConflictResolutionResult:
        """Record the stale attempt, supersede and open the bound successor."""
        del conflict_row
        if conflict.source_id is not None:
            await self._insert_resolution_event(
                connection,
                command,
                workspace_id,
                source_id=conflict.source_id,
                device_id=conflict.originating_device_id,
                committed_version_id=None,
            )
        await connection.execute(
            sa.insert(source_conflicts).values(
                conflict_id=identities.successor_conflict_id,
                workspace_id=workspace_id,
                source_id=conflict.source_id,
                conflict_kind=conflict.conflict_kind.value,
                status=ConflictStatus.OPEN.value,
                originating_event_id=command.resolution_event_id,
                originating_device_id=conflict.originating_device_id,
                capture_idempotency_key=command.idempotency_key.value,
                base_version_id=conflict.base_version_id,
                observed_remote_version_id=source_state.current_version_id,
                candidate_kind=conflict.candidate.candidate_kind.value,
                verified_candidate_object_id=conflict.candidate.verified_candidate_object_id,
                normalized_locator=await self._stored_locator(connection, conflict.conflict_id),
                captured_at=now,
            )
        )
        self._require_exactly_one_row(
            await connection.execute(
                sa.update(source_conflicts)
                .values(
                    status=ConflictStatus.SUPERSEDED.value,
                    resolution_kind=command.resolution_kind.value,
                    resolution_event_id=command.resolution_event_id,
                    resolution_idempotency_key=command.idempotency_key.value,
                    successor_conflict_id=identities.successor_conflict_id,
                    closed_at=now,
                )
                .where(
                    source_conflicts.c.conflict_id == conflict.conflict_id,
                    source_conflicts.c.workspace_id == workspace_id,
                    source_conflicts.c.status == ConflictStatus.OPEN.value,
                )
            )
        )
        await self._insert_conflict_audit(
            connection,
            workspace_id=workspace_id,
            device_id=conflict.originating_device_id,
            action=SUPERSEDED_AUDIT_ACTION,
            target_conflict_id=conflict.conflict_id,
            diagnostic_context=diagnostic_context,
            audit_event_id=identities.audit_event_id,
        )
        successor = SourceConflict(
            conflict_id=identities.successor_conflict_id,
            workspace_id=workspace_id,
            source_id=conflict.source_id,
            conflict_kind=conflict.conflict_kind,
            status=ConflictStatus.OPEN,
            originating_event_id=command.resolution_event_id,
            originating_device_id=conflict.originating_device_id,
            base_version_id=conflict.base_version_id,
            observed_remote_version_id=source_state.current_version_id,
            candidate=conflict.candidate,
            captured_at=now,
            resolution_kind=None,
            resolution_event_id=None,
            resulting_version_id=None,
            successor_conflict_id=None,
            closed_at=None,
        )
        return ConflictResolutionResult(
            kind=ConflictResolutionOutcome.STALE_SUCCESSOR,
            conflict_id=conflict.conflict_id,
            resolution_event_id=command.resolution_event_id,
            resolution_kind=command.resolution_kind,
            resulting_version_id=None,
            successor=successor,
            completed_at=now,
        )

    async def _winner_transition(
        self,
        connection: AsyncConnection,
        command: ResolveConflictCommand,
        workspace_id: UUID,
        conflict: SourceConflict,
        conflict_row: _ConflictRowMapping,
        source_state: _LockedSourceState,
        identities: ConflictResolutionIdentities,
        diagnostic_context: DiagnosticContext,
        now: datetime,
    ) -> ConflictResolutionResult:
        """Accept the winner and close the conflict, or reject typed."""
        del conflict_row
        if command.resolution_kind not in VERSION_PUBLISHING_RESOLUTIONS:
            # keep_remote: the reviewed remote already IS current; closing the
            # conflict publishes nothing and moves no pointer.
            if conflict.source_id is not None:
                await self._insert_resolution_event(
                    connection,
                    command,
                    workspace_id,
                    source_id=conflict.source_id,
                    device_id=conflict.originating_device_id,
                    committed_version_id=command.reviewed_remote_version_id,
                )
            self._require_exactly_one_row(
                await connection.execute(
                    sa.update(source_conflicts)
                    .values(
                        status=ConflictStatus.RESOLVED.value,
                        resolution_kind=command.resolution_kind.value,
                        resolution_event_id=command.resolution_event_id,
                        resolution_idempotency_key=command.idempotency_key.value,
                        resulting_version_id=None,
                        closed_at=now,
                    )
                    .where(
                        source_conflicts.c.conflict_id == conflict.conflict_id,
                        source_conflicts.c.workspace_id == workspace_id,
                        source_conflicts.c.status == ConflictStatus.OPEN.value,
                    )
                )
            )
            await self._insert_conflict_audit(
                connection,
                workspace_id=workspace_id,
                device_id=conflict.originating_device_id,
                action=RESOLUTION_AUDIT_ACTION,
                target_conflict_id=conflict.conflict_id,
                diagnostic_context=diagnostic_context,
                audit_event_id=identities.audit_event_id,
            )
            return ConflictResolutionResult(
                kind=ConflictResolutionOutcome.RESOLVED,
                conflict_id=conflict.conflict_id,
                resolution_event_id=command.resolution_event_id,
                resolution_kind=command.resolution_kind,
                resulting_version_id=None,
                successor=None,
                completed_at=now,
            )
        if conflict.source_id is None:
            raise SourceConflictError(
                ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
                safe_details={"reason": SOURCE_ID_INVALID},
            )
        if source_state.sync_state not in _PUBLISHABLE_SOURCE_STATES:
            raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID)
        winning_object_id = (
            conflict.candidate.verified_candidate_object_id
            if command.resolution_kind is ConflictResolutionKind.KEEP_LOCAL
            else command.verified_candidate_object_id
        )
        if conflict.candidate.candidate_kind is not ConflictCandidateKind.CONTENT or (
            winning_object_id is None
        ):
            # A keep_local resolution acts on the retained content candidate;
            # applying a deletion intent is lifecycle-domain work.
            raise SourceConflictError(
                ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
                safe_details={"reason": CANDIDATE_INVALID},
            )
        object_result = await connection.execute(
            sa.select(content_objects.c.content_object_id).where(
                content_objects.c.content_object_id == winning_object_id
            )
        )
        if object_result.scalar_one_or_none() is None:
            raise SourceConflictError(
                ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
                safe_details={"reason": CANDIDATE_OBJECT_INVALID},
            )
        parent_version_id = source_state.current_version_id
        next_ordinal = 1
        if parent_version_id is not None:
            ordinal_result = await connection.execute(
                sa.select(source_versions.c.content_version).where(
                    source_versions.c.workspace_id == workspace_id,
                    source_versions.c.source_id == conflict.source_id,
                    source_versions.c.source_version_id == parent_version_id,
                )
            )
            stored_ordinal = ordinal_result.scalar_one_or_none()
            if stored_ordinal is None or int(stored_ordinal) < 1:
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID)
            next_ordinal = int(stored_ordinal) + 1
        await connection.execute(
            sa.insert(source_versions).values(
                source_version_id=identities.resulting_version_id,
                workspace_id=workspace_id,
                source_id=conflict.source_id,
                content_object_id=winning_object_id,
                content_version=next_ordinal,
                parent_version_id=parent_version_id,
                author_kind=_AUTHOR_KIND_DEVICE,
                author_id=conflict.originating_device_id,
            )
        )
        pointer_guard = (
            sources.c.current_version_id == command.reviewed_remote_version_id
            if command.reviewed_remote_version_id is not None
            else sources.c.current_version_id.is_(None)
        )
        self._require_exactly_one_row(
            await connection.execute(
                sa.update(sources)
                .values(
                    current_version_id=identities.resulting_version_id,
                    updated_at=sa.text("CURRENT_TIMESTAMP"),
                )
                .where(
                    sources.c.source_id == conflict.source_id,
                    sources.c.workspace_id == workspace_id,
                    pointer_guard,
                )
            )
        )
        await self._insert_resolution_event(
            connection,
            command,
            workspace_id,
            source_id=conflict.source_id,
            device_id=conflict.originating_device_id,
            committed_version_id=identities.resulting_version_id,
        )
        await self._insert_projection_intent(
            connection,
            workspace_id,
            command.resolution_event_id,
            conflict.source_id,
            identities.resulting_version_id,
            identities.qdrant_intent_id,
            PROJECTION_KIND_QDRANT,
        )
        await self._insert_projection_intent(
            connection,
            workspace_id,
            command.resolution_event_id,
            conflict.source_id,
            identities.resulting_version_id,
            identities.neo4j_intent_id,
            PROJECTION_KIND_NEO4J,
        )
        self._require_exactly_one_row(
            await connection.execute(
                sa.update(source_conflicts)
                .values(
                    status=ConflictStatus.RESOLVED.value,
                    resolution_kind=command.resolution_kind.value,
                    resolution_event_id=command.resolution_event_id,
                    resolution_idempotency_key=command.idempotency_key.value,
                    resulting_version_id=identities.resulting_version_id,
                    closed_at=now,
                )
                .where(
                    source_conflicts.c.conflict_id == conflict.conflict_id,
                    source_conflicts.c.workspace_id == workspace_id,
                    source_conflicts.c.status == ConflictStatus.OPEN.value,
                )
            )
        )
        await self._insert_conflict_audit(
            connection,
            workspace_id=workspace_id,
            device_id=conflict.originating_device_id,
            action=RESOLUTION_AUDIT_ACTION,
            target_conflict_id=conflict.conflict_id,
            diagnostic_context=diagnostic_context,
            audit_event_id=identities.audit_event_id,
        )
        return ConflictResolutionResult(
            kind=ConflictResolutionOutcome.RESOLVED,
            conflict_id=conflict.conflict_id,
            resolution_event_id=command.resolution_event_id,
            resolution_kind=command.resolution_kind,
            resulting_version_id=identities.resulting_version_id,
            successor=None,
            completed_at=now,
        )

    @staticmethod
    async def _insert_resolution_event(
        connection: AsyncConnection,
        command: ResolveConflictCommand,
        workspace_id: UUID,
        *,
        source_id: UUID,
        device_id: UUID,
        committed_version_id: UUID | None,
    ) -> None:
        await connection.execute(
            sa.insert(sync_events).values(
                event_id=command.resolution_event_id,
                workspace_id=workspace_id,
                source_id=source_id,
                device_id=device_id,
                committed_version_id=committed_version_id,
                base_version_id=command.reviewed_remote_version_id,
                idempotency_key=command.idempotency_key.value,
                request_fingerprint=compute_resolution_request_fingerprint(command),
                event_type=RESOLUTION_EVENT_TYPE,
            )
        )

    @staticmethod
    async def _insert_projection_intent(
        connection: AsyncConnection,
        workspace_id: UUID,
        event_id: UUID,
        source_id: UUID,
        source_version_id: UUID,
        projection_intent_id: UUID,
        projection_kind: str,
    ) -> None:
        await connection.execute(
            sa.insert(projection_intents).values(
                projection_intent_id=projection_intent_id,
                workspace_id=workspace_id,
                event_id=event_id,
                source_id=source_id,
                source_version_id=source_version_id,
                projection_kind=projection_kind,
                operation=PROJECTION_OPERATION_UPSERT,
            )
        )

    async def _select_locked_source_state(
        self, connection: AsyncConnection, workspace_id: UUID, source_id: UUID | None
    ) -> _LockedSourceState:
        """Lock and read the current source state for the resolution recheck."""
        if source_id is None:
            return _LockedSourceState(sync_state=None, current_version_id=None, deleted_at=None)
        await connection.execute(source_lock_statement(source_id))
        result = await connection.execute(
            sa.select(
                sources.c.sync_state,
                sources.c.current_version_id,
                sources.c.deleted_at,
            )
            .where(
                sources.c.source_id == source_id,
                sources.c.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        row = result.one_or_none()
        if row is None:
            raise SourceConflictError(
                ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
                safe_details={"reason": SOURCE_ID_INVALID},
            )
        return _LockedSourceState(
            sync_state=row.sync_state,
            current_version_id=row.current_version_id,
            deleted_at=row.deleted_at,
        )

    async def _stored_locator(self, connection: AsyncConnection, conflict_id: UUID) -> str | None:
        result = await connection.execute(
            sa.select(source_conflicts.c.normalized_locator).where(
                source_conflicts.c.conflict_id == conflict_id
            )
        )
        stored = result.scalar_one()
        return None if stored is None else str(stored)

    @staticmethod
    async def _fetch_conflict_row(
        connection: AsyncConnection, statement: sa.Select[tuple[Any, ...]]
    ) -> _ConflictRowMapping | None:
        result = await connection.execute(statement)
        return cast("_ConflictRowMapping | None", result.mappings().one_or_none())

    @staticmethod
    def _require_exactly_one_row(result: Any) -> None:
        if result.rowcount != 1:
            raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID)


def _require_resolution_kind(value: Any) -> ConflictResolutionKind:
    """Resolve the closed resolution kind of a stored closed row, fail-closed."""
    if value is None:
        raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID)
    kind = _RESOLUTION_TOKEN_TO_KIND.get(str(value))
    if kind is None:
        raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID)
    return kind


__all__ = [
    "CAPTURE_AUDIT_ACTION",
    "CONFLICT_AUDIT_TARGET_KIND",
    "MAX_OPEN_CONFLICT_PAGE",
    "RESOLUTION_AUDIT_ACTION",
    "SUPERSEDED_AUDIT_ACTION",
    "ConflictCaptureIdentities",
    "ConflictResolutionIdentities",
    "PostgresqlSourceConflictStore",
    "SourceConflictDatabaseRetryPolicy",
    "capture_replay_by_key_statement",
    "captured_conflict_by_event_statement",
    "compute_capture_request_fingerprint",
    "compute_resolution_request_fingerprint",
    "conflict_read_statement",
    "hydrate_source_conflict",
    "list_open_conflicts_statement",
    "map_source_conflict_database_failure",
]
