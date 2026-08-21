"""Atomic PostgreSQL lifecycle transition adapter for rename, move, delete and restore.

:class:`PostgresqlSourceLifecycleStore` implements the
:class:`~personal_os.source_lifecycle.ports.SourceLifecycleStore` port over
the migrated ``20260820_01`` canonical baseline. ``resolve_committed``
performs the lock-free indexed exact-replay lookup (spec 9.3): revalidate
the credential-derived workspace identity, search ``(workspace_id,
event_id, idempotency_key)``, then the globally unique ``event_id``; an
exact fingerprint replay returns the original hydrated commit result
without mutation. ``commit`` runs one ``READ COMMITTED`` transaction
behind the same locked prefix the publication store uses (idempotency
identity, source advisory lock and row, locator advisory locks in
canonical text order with their rows, optional tombstone row), then
validates state/version/locator/classification/availability/policy and
performs the atomic transition: close the prior locator (delete/restore
also drop the tombstone), open the target locator (or tombstone), insert
the lifecycle ``sync_events`` row, two ``projection_intents`` rows and one
redacted ``audit_events`` row, all in one commit. Locked policy is
re-evaluated against the target locator, never trusting the externally
passed decision alone; a policy revision mismatch falls through the
typed :class:`SourceLifecycleError` with the closed vocabulary, never
mutating canonical state.

Driver failures are routed through
:mod:`postgresql_source_store.error_mapping` so SQLSTATE, SQL, parameters
and driver text never leave the adapter. Lock conflicts, serialization
failures and connection-class unavailability trigger the bounded
three-attempt cancellable jitter retry; business and integrity failures
never retry. An ambiguous acknowledgement (a connection-class failure
during the commit window) discards the connection and runs a bounded
fresh-connection replay lookup: evidence returns the replay, absence
raises the retryable ``source_lifecycle_commit_outcome_unknown``. No
network I/O, Temporal call or provider SDK ever runs inside the
transaction path.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import EventName
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.exclusion_policy.enforcement import (
    PolicyTrustAnchorVerifier,
    evaluate_policy_decision,
    parse_verified_policy_revision,
)
from personal_os.source_lifecycle.commands import (
    LifecycleOperation,
    LifecycleState,
    SourceLifecycleCommand,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.errors import (
    SourceLifecycleError,
    SourceLifecycleErrorCode,
)
from personal_os.source_lifecycle.fingerprint import (
    LifecycleRequestFingerprint,
    fingerprint_lifecycle_command,
)
from personal_os.source_lifecycle.ports import (
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
)
from personal_os.source_lifecycle.title import derive_title_v1
from personal_os.source_locators import NormalizedLocator
from personal_os.sources.actors import SourceActor
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import (
    DatabaseRetryPolicy,
)
from postgresql_source_store.locks import (
    SOURCE_LOCK_NAMESPACE,
    advisory_xact_lock_statement,
    idempotency_lock_key,
    idempotency_lock_statement,
    signed_first_sha256_word,
    source_lock_statement,
)
from postgresql_source_store.policy_enforcement import (
    load_locked_active_policy_snapshot,
)
from postgresql_source_store.projection_intents import (
    intent_insert_statement as _projection_intent_insert_statement,
)
from postgresql_source_store.tables import (
    audit_events,
    source_locators,
    source_tombstones,
    sources,
    sync_events,
)

#: Audit actions per operation; mirrors the spec's success actions and the
#: rejection action's ``.rejected`` suffix on the diagnostic sink.
AUDIT_ACTIONS_BY_OPERATION: Final[Mapping[LifecycleOperation, str]] = {
    LifecycleOperation.RENAME: "source.locator_renamed",
    LifecycleOperation.MOVE: "source.locator_moved",
    LifecycleOperation.DELETE: "source.deleted",
    LifecycleOperation.RESTORE: "source.restored",
}

#: Source ``sync_events.event_type`` literals for the four transitions.
EVENT_TYPE_BY_OPERATION: Final[Mapping[LifecycleOperation, str]] = {
    LifecycleOperation.RENAME: "rename",
    LifecycleOperation.MOVE: "move",
    LifecycleOperation.DELETE: "delete",
    LifecycleOperation.RESTORE: "restore",
}

#: Closed closed projection kind literals.
PROJECTION_KIND_QDRANT: Final[str] = "qdrant"
PROJECTION_KIND_NEO4J: Final[str] = "neo4j"

#: Closed projection operation literals.
PROJECTION_OPERATION_UPSERT: Final[str] = "upsert"
PROJECTION_OPERATION_DELETE: Final[str] = "delete"

#: Target audit ``target_kind`` for in-transaction success audits.
AUDIT_TARGET_KIND_SOURCE: Final[str] = "source"

#: Canonical ``sources.sync_state`` values used by the lifecycle transitions.
SOURCE_STATE_ACTIVE: Final[str] = "active"
SOURCE_STATE_DELETED: Final[str] = "deleted"

#: Rejection audit reason codes for the closed error vocabulary.
REASON_LOCATOR_CONFLICT: Final[str] = "locator_conflict"
REASON_LOCATOR_MISSING: Final[str] = "locator_missing"
REASON_VERSION_CONFLICT: Final[str] = "version_conflict"
REASON_TOMBSTONE_NOT_FOUND: Final[str] = "tombstone_not_found"
REASON_TOMBSTONE_CLOSED: Final[str] = "tombstone_closed"
REASON_IDEMPOTENCY_MISMATCH: Final[str] = "idempotency_mismatch"
REASON_EVENT_IDENTITY_MISMATCH: Final[str] = "event_identity_mismatch"
REASON_CLASSIFICATION_MISMATCH: Final[str] = "classification_mismatch"

#: Lock namespace for locator advisory locks. Reuses the source namespace
#: hash-bucket scheme so unrelated namespaces never collide.
LOCATOR_LOCK_NAMESPACE: Final[int] = SOURCE_LOCK_NAMESPACE ^ 0x4C43  # "LC"

#: Stable safe-diff hash contract tag for the lifecycle audit row.
LIFECYCLE_SAFE_DIFF_CONTRACT: Final[str] = "source_lifecycle_diff/v1"

#: Closed diagnostic event names consumed by the rejection audit sink.
LIFECYCLE_REJECTION_DIAGNOSTIC: Final[EventName] = EventName.SOURCE_VERSION_PUBLISH_REJECTED


@dataclass(frozen=True, slots=True)
class LifecycleCommitIdentities:
    """Backend UUIDv7 identities for one lifecycle commit service invocation.

    The identities are allocated once per call and reused through every
    retry so a contention retry rewrites the same canonical identity
    rather than leaking one per attempt. The event identity comes from
    the command itself; only the auxiliary rows are pre-allocated here.
    """

    source_locator_id: UUID
    tombstone_id: UUID | None
    qdrant_intent_id: UUID
    neo4j_intent_id: UUID
    audit_event_id: UUID
    content_object_id: UUID | None

    @classmethod
    def allocate(
        cls, *, include_tombstone: bool, include_locator: bool = True
    ) -> LifecycleCommitIdentities:
        """Allocate the seven deterministic identities for one commit.

        A delete that closes an existing locator must not mint a new
        ``source_locator_id`` (the row already exists); a restore that
        opens a target locator always mints one.
        """

        return cls(
            source_locator_id=uuid7() if include_locator else UUID(int=0),
            tombstone_id=uuid7() if include_tombstone else None,
            qdrant_intent_id=uuid7(),
            neo4j_intent_id=uuid7(),
            audit_event_id=uuid7(),
            content_object_id=None,
        )


@dataclass(frozen=True, slots=True)
class LifecycleReplayLookupRow:
    """The hydrated lookup row used by ``resolve_committed``.

    Carries the minimum evidence the exact replay needs to return a
    committed result: workspace and source identifiers, event sequence and
    type, the committed version, the resulting locator or tombstone id
    and the committed timestamp. Locator text is never carried; the
    fingerprint lookup only needs the resolved value.
    """

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
    resulting_locator_value: str | None
    tombstone_id: UUID | None

    @classmethod
    def from_result_row(cls, row: Any) -> LifecycleReplayLookupRow:
        """Build the typed row from one named SQLAlchemy result row."""

        committed_at = row["committed_at"]
        if not isinstance(committed_at, datetime) or committed_at.tzinfo is None:
            raise SourceLifecycleError(SourceLifecycleErrorCode.COMMIT_OUTCOME_UNKNOWN)
        sequence = int(row["event_sequence"])
        if sequence < 1:
            raise SourceLifecycleError(SourceLifecycleErrorCode.COMMIT_OUTCOME_UNKNOWN)
        return cls(
            workspace_id=row["workspace_id"],
            source_id=row["source_id"],
            event_id=row["event_id"],
            event_sequence=sequence,
            event_type=str(row["event_type"]),
            base_version_id=row["base_version_id"],
            committed_version_id=row["committed_version_id"],
            idempotency_key=str(row["idempotency_key"]),
            request_fingerprint=str(row["request_fingerprint"]),
            committed_at=committed_at,
            resulting_locator_value=row["resulting_locator"],
            tombstone_id=row["tombstone_id"],
        )


def advisory_lock_key_for_locator(locator: NormalizedLocator) -> int:
    """Derive the transaction lock key for one canonical locator value.

    The material is the locator text bytes sealed by a NUL byte that
    cannot occur in any normalized locator; the hash interpretation is
    identical to every other advisory-lock family in this package so
    derived keys stay bounded ``int32``.
    """

    return signed_first_sha256_word(locator.value.encode("utf-8"))


def locator_advisory_lock_statement(locator: NormalizedLocator) -> sa.TextClause:
    """Build the transaction-scoped advisory lock for one locator."""

    return advisory_xact_lock_statement(
        LOCATOR_LOCK_NAMESPACE,
        advisory_lock_key_for_locator(locator),
    )


def order_locator_lock_keys(
    locators: list[NormalizedLocator],
) -> list[tuple[NormalizedLocator, int]]:
    """Sort the locator advisory-lock pairs into canonical text order.

    The lock order is fixed: ``min(value) < max(value)`` so two
    transactions that touch the same pair of locators always acquire the
    locks in the same order, eliminating the deadlock window.
    """

    return sorted(
        ((locator, advisory_lock_key_for_locator(locator)) for locator in locators),
        key=lambda pair: pair[0].value,
    )


def is_locator_lock_order_valid(
    ordered: list[tuple[NormalizedLocator, int]],
) -> bool:
    """Return whether the supplied locator list is sorted canonically."""

    values = [locator.value for locator, _key in ordered]
    return values == sorted(values)


def sync_event_lookup_by_key_statement(
    workspace_id: UUID, idempotency_key: str
) -> sa.Select[tuple[Any, ...]]:
    """Build the parameter-bound replay lookup by ``(workspace_id, idempotency_key)``.

    The join surfaces the resulting locator or tombstone identity, the
    committed version and the canonical ``sources.sync_state`` so the
    adapter can return a hydrated commit result without a second query.
    """

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
            source_locators.c.normalized_locator.label("resulting_locator"),
            source_tombstones.c.source_tombstone_id.label("tombstone_id"),
        )
        .select_from(sync_events)
        .outerjoin(
            source_locators,
            sa.and_(
                source_locators.c.opened_event_id == sync_events.c.event_id,
                source_locators.c.workspace_id == sync_events.c.workspace_id,
                source_locators.c.source_id == sync_events.c.source_id,
            ),
        )
        .outerjoin(
            source_tombstones,
            sa.and_(
                source_tombstones.c.delete_event_id == sync_events.c.event_id,
                source_tombstones.c.workspace_id == sync_events.c.workspace_id,
                source_tombstones.c.source_id == sync_events.c.source_id,
            ),
        )
        .where(
            sync_events.c.workspace_id == workspace_id,
            sync_events.c.idempotency_key == idempotency_key,
        )
    )


def sync_event_lookup_by_event_statement(event_id: UUID) -> sa.Select[tuple[Any, ...]]:
    """Build the global, parameter-bound event-identity lookup.

    Used by the replay path's identity-mismatch branch and by the
    service-level replay sanity check: a duplicate ``event_id`` under a
    different ``(workspace_id, idempotency_key)`` tuple is a closed
    ``source_event_identity_mismatch`` rejection.
    """

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
            source_locators.c.normalized_locator.label("resulting_locator"),
            source_tombstones.c.source_tombstone_id.label("tombstone_id"),
        )
        .select_from(sync_events)
        .outerjoin(
            source_locators,
            sa.and_(
                source_locators.c.opened_event_id == sync_events.c.event_id,
                source_locators.c.workspace_id == sync_events.c.workspace_id,
                source_locators.c.source_id == sync_events.c.source_id,
            ),
        )
        .outerjoin(
            source_tombstones,
            sa.and_(
                source_tombstones.c.delete_event_id == sync_events.c.event_id,
                source_tombstones.c.workspace_id == sync_events.c.workspace_id,
                source_tombstones.c.source_id == sync_events.c.source_id,
            ),
        )
        .where(sync_events.c.event_id == event_id)
    )


def tombstone_lookup_by_id_statement(
    *, workspace_id: UUID, source_tombstone_id: UUID
) -> sa.Select[tuple[Any, ...]]:
    """Build the parameter-bound tombstone-by-id lookup."""

    return sa.select(
        source_tombstones.c.source_tombstone_id,
        source_tombstones.c.workspace_id,
        source_tombstones.c.source_id,
        source_tombstones.c.delete_event_id,
        source_tombstones.c.retained_version_id,
        source_tombstones.c.retained_locator,
        source_tombstones.c.actor_kind,
        source_tombstones.c.actor_id,
        source_tombstones.c.deleted_at,
        source_tombstones.c.restore_event_id,
        source_tombstones.c.restored_at,
    ).where(
        source_tombstones.c.workspace_id == workspace_id,
        source_tombstones.c.source_tombstone_id == source_tombstone_id,
    )


def locator_open_insert_statement(
    *,
    source_locator_id: UUID,
    workspace_id: UUID,
    source_id: UUID,
    locator: NormalizedLocator,
    opened_event_id: UUID,
    opened_sequence: int,
) -> sa.Insert:
    """Build the parameter-bound insert of one new ``source_locators`` row.

    The opening event identity is the lifecycle event being committed;
    ``display_locator`` mirrors the canonical normalized value until a
    future display upgrade runs.
    """

    return (
        sa.insert(source_locators)
        .values(
            source_locator_id=source_locator_id,
            workspace_id=workspace_id,
            source_id=source_id,
            normalized_locator=locator.value,
            display_locator=locator.value,
            opened_event_id=opened_event_id,
            opened_sequence=opened_sequence,
        )
        .returning(source_locators.c.source_locator_id)
    )


def close_locator_statement(
    *, source_locator_id: UUID, closed_event_id: UUID, closed_sequence: int
) -> sa.Update:
    """Build the guarded update that closes one ``source_locators`` row.

    The guard admits only the still-open row by its primary key; a stale
    closure (already closed by an earlier winner) is reported as a
    zero-row update by the adapter.
    """

    return (
        sa.update(source_locators)
        .values(
            closed_event_id=closed_event_id,
            closed_sequence=closed_sequence,
            closed_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            source_locators.c.source_locator_id == source_locator_id,
            source_locators.c.closed_event_id.is_(None),
        )
    )


def open_tombstone_insert_statement(
    *,
    source_tombstone_id: UUID,
    workspace_id: UUID,
    source_id: UUID,
    delete_event_id: UUID,
    retained_version_id: UUID,
    retained_locator: NormalizedLocator,
    actor_kind: str,
    actor_id: UUID,
) -> sa.Insert:
    """Build the parameter-bound tombstone insert for one delete."""

    return sa.insert(source_tombstones).values(
        source_tombstone_id=source_tombstone_id,
        workspace_id=workspace_id,
        source_id=source_id,
        delete_event_id=delete_event_id,
        retained_version_id=retained_version_id,
        retained_locator=retained_locator.value,
        actor_kind=actor_kind,
        actor_id=actor_id,
    )


def tombstone_close_statement(*, source_tombstone_id: UUID, restore_event_id: UUID) -> sa.Update:
    """Build the guarded update that closes one tombstone via restore.

    The guard admits only the still-open tombstone; a concurrent restore
    wins and this update returns zero rows.
    """

    return (
        sa.update(source_tombstones)
        .values(
            restore_event_id=restore_event_id,
            restored_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            source_tombstones.c.source_tombstone_id == source_tombstone_id,
            source_tombstones.c.restore_event_id.is_(None),
        )
    )


def close_tombstone_set_delete(source_id: UUID) -> sa.Update:
    """Build the guarded ``sources.sync_state=deleted`` and timestamp write.

    The guard admits only the active source by its primary key; a
    concurrent terminal winner makes the update a zero-row transition
    that the adapter treats as a state conflict.
    """

    return (
        sa.update(sources)
        .values(
            sync_state=SOURCE_STATE_DELETED,
            deleted_at=sa.text("CURRENT_TIMESTAMP"),
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            sources.c.source_id == source_id,
            sources.c.sync_state == SOURCE_STATE_ACTIVE,
        )
    )


def restore_source_update_statement(source_id: UUID) -> sa.Update:
    """Build the guarded ``sources.sync_state=active`` and ``deleted_at=NULL`` write."""

    return (
        sa.update(sources)
        .values(
            sync_state=SOURCE_STATE_ACTIVE,
            deleted_at=None,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            sources.c.source_id == source_id,
            sources.c.sync_state == SOURCE_STATE_DELETED,
        )
    )


def rename_title_update_statement(*, source_id: UUID, title_value: str) -> sa.Update:
    """Build the guarded ``sources.title`` write for a rename."""

    return (
        sa.update(sources)
        .values(
            title=title_value,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            sources.c.source_id == source_id,
        )
    )


def event_insert_statement(
    *,
    event_id: UUID,
    workspace_id: UUID,
    source_id: UUID,
    device_id: UUID | None,
    committed_version_id: UUID,
    base_version_id: UUID,
    idempotency_key: str,
    request_fingerprint: str,
    event_type: str,
    client_timestamp: datetime | None,
) -> sa.Insert:
    """Build the parameter-bound ``sync_events`` insert for one transition.

    ``base_version_id`` is the source's ``current_version_id`` at the
    moment of commit (the expected version); the inserted event sequence
    and ``committed_at`` are PostgreSQL-owned via ``returning``.
    """

    return (
        sa.insert(sync_events)
        .values(
            event_id=event_id,
            workspace_id=workspace_id,
            source_id=source_id,
            device_id=device_id,
            committed_version_id=committed_version_id,
            base_version_id=base_version_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            event_type=event_type,
            client_timestamp=client_timestamp,
        )
        .returning(sync_events.c.event_sequence, sync_events.c.committed_at)
    )


def intent_insert_statement(
    *,
    projection_intent_id: UUID,
    workspace_id: UUID,
    event_id: UUID,
    source_id: UUID,
    source_version_id: UUID,
    projection_kind: str,
    operation: str,
) -> sa.Insert:
    """Build the parameter-bound ``projection_intents`` insert.

    Re-exported from :mod:`postgresql_source_store.projection_intents` so
    the lifecycle adapter shares the same statement helper as the
    projection-intent lease store. The single source of truth lives in
    ``projection_intents.intent_insert_statement``.
    """

    return _projection_intent_insert_statement(
        projection_intent_id=projection_intent_id,
        workspace_id=workspace_id,
        event_id=event_id,
        source_id=source_id,
        source_version_id=source_version_id,
        projection_kind=projection_kind,
        operation=operation,
    )


def audit_insert_statement(
    *,
    audit_event_id: UUID,
    workspace_id: UUID,
    actor_kind: str,
    actor_id: UUID | None,
    action: str,
    target_kind: str,
    target_id: UUID,
    request_id: UUID,
    client_request_id: UUID | None,
    trace_id: str,
    result: str,
    reason_code: str | None,
    safe_diff_hash: str,
) -> sa.Insert:
    """Build the parameter-bound ``audit_events`` insert for one transition.

    The audit row carries only the canonical identifiers and the safe
    diff digest (computed in :func:`_compute_safe_diff_digest`); no raw
    locator, locator fingerprint, title or content digest enters the
    row.
    """

    return sa.insert(audit_events).values(
        audit_event_id=audit_event_id,
        workspace_id=workspace_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        actor_reference=None,
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        request_id=request_id,
        client_request_id=client_request_id,
        trace_id=trace_id,
        result=result,
        reason_code=reason_code,
        safe_diff_hash=safe_diff_hash,
    )


def idempotency_key_for_command(workspace_id: UUID, command: SourceLifecycleCommand) -> int:
    """Derive the idempotency identity lock key from the workspace + command key."""

    return idempotency_lock_key(workspace_id, _wrap_idempotency_key(command.idempotency_key))


def _wrap_idempotency_key(value: str) -> Any:
    from personal_os.sources.commands import IdempotencyKey

    return IdempotencyKey(value)


# --- classifier helpers ------------------------------------------------------


def classify_locator_conflict(
    *,
    expected: NormalizedLocator | None,
    actual: NormalizedLocator | None,
) -> SourceLifecycleError | None:
    """Return the typed error when the active locator disagrees with expected.

    A missing active locator is :class:`SourceLifecycleErrorCode.LOCATOR_MISSING`;
    a divergent locator is :class:`SourceLifecycleErrorCode.LOCATOR_CONFLICT`.
    """

    if expected is None:
        return None
    if actual is None:
        return SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_MISSING)
    if actual.value != expected.value:
        return SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT)
    return None


def classify_version_mismatch(*, expected: UUID, actual: UUID) -> SourceLifecycleError | None:
    """Return the typed version-conflict error when the expected id differs."""

    if actual != expected:
        return SourceLifecycleError(SourceLifecycleErrorCode.VERSION_CONFLICT)
    return None


def classify_state_mismatch(
    *, actual_state: str, operation: LifecycleOperation
) -> SourceLifecycleError | None:
    """Return the typed state error for a forbidden source-state transition.

    Rename/move/delete require an active source; restore requires a
    deleted source. A missing source (``actual_state == None``) is the
    closed ``source_not_found`` mapped onto
    :class:`SourceLifecycleErrorCode.LOCATOR_MISSING`.
    """

    if actual_state == SOURCE_STATE_ACTIVE:
        if operation is LifecycleOperation.RESTORE:
            return SourceLifecycleError(SourceLifecycleErrorCode.TOMBSTONE_NOT_FOUND)
        return None
    if actual_state == SOURCE_STATE_DELETED:
        if operation is LifecycleOperation.RESTORE:
            return None
        return SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_MISSING)
    return SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_MISSING)


def classify_tombstone_conflict(
    *,
    tombstone_present: bool,
    tombstone_already_restored: bool,
    tombstone_id: UUID,
) -> SourceLifecycleError | None:
    """Return the typed tombstone error for a restore against a missing/closed tombstone."""

    if not tombstone_present:
        return SourceLifecycleError(SourceLifecycleErrorCode.TOMBSTONE_NOT_FOUND)
    if tombstone_already_restored:
        return SourceLifecycleError(SourceLifecycleErrorCode.TOMBSTONE_CLOSED)
    del tombstone_id
    return None


def classify_classification(
    *,
    operation: LifecycleOperation,
    expected_locator: NormalizedLocator,
    target_locator: NormalizedLocator,
) -> SourceLifecycleError | None:
    """Return the typed classification error if rename/move is mislabeled.

    A rename keeps the same parent segment; a move changes the parent.
    A mismatched classification is the closed
    :class:`SourceLifecycleErrorCode.LOCATOR_CONFLICT` rejection.
    """

    expected_parent = expected_locator.value.rsplit("/", 1)[0]
    target_parent = target_locator.value.rsplit("/", 1)[0]
    same_parent = expected_parent == target_parent
    if operation is LifecycleOperation.RENAME and not same_parent:
        return SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT)
    if operation is LifecycleOperation.MOVE and same_parent:
        return SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT)
    return None


def classify_target_availability(
    *,
    target_held_by_other_source: bool,
    target_locator: NormalizedLocator,
) -> SourceLifecycleError | None:
    """Return the typed target-held error when another active source owns the path."""

    del target_locator
    if target_held_by_other_source:
        return SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT)
    return None


# --- intent helpers ----------------------------------------------------------


def _projection_intent_operation_for(
    command: SourceLifecycleCommand, decision: LifecyclePolicyDecision
) -> str:
    """Return the projection operation string for one command and decision."""

    if command.operation is LifecycleOperation.DELETE:
        return PROJECTION_OPERATION_DELETE
    if decision.outcome is LifecyclePolicyOutcome.ALLOWED:
        return PROJECTION_OPERATION_UPSERT
    return PROJECTION_OPERATION_DELETE


# --- diagnostic helpers ------------------------------------------------------


def _diagnostic_fields_for_rename_rejection(
    *,
    command: SourceLifecycleCommand,
    decision: LifecyclePolicyDecision,
    diagnostic_context: DiagnosticContext,
    error_code: SourceLifecycleErrorCode,
    duration_seconds: float,
) -> dict[str, Any]:
    """Build the closed-vocabulary diagnostic fields for one rejection.

    No raw locator, locator fingerprint, title or content digest enters
    the fields; only canonical UUIDs, the operation token, the safe diff
    digest, the closed error code and the duration cross the boundary.
    """

    return {
        "request_id": diagnostic_context.request_id,
        "trace_id": diagnostic_context.trace.trace_id.value,
        "operation": command.operation.value,
        "error_code": error_code.value,
        "policy_revision_number": decision.policy_revision_number,
        "duration_seconds": duration_seconds,
        "safe_diff_hash": _compute_safe_diff_digest(
            command=command,
            decision=decision,
            result=None,
        ),
    }


def _compute_safe_diff_digest(
    *,
    command: SourceLifecycleCommand,
    decision: LifecyclePolicyDecision,
    result: SourceLifecycleCommitResult | None,
) -> str:
    """Hash only canonical IDs, the operation and the policy outcome.

    Raw locator values never enter the digest; the locator fingerprint
    (a hash) is also excluded so the audit row discloses nothing a
    future tenant or device can correlate back to a path.
    """

    envelope: dict[str, object] = {
        "contract": LIFECYCLE_SAFE_DIFF_CONTRACT,
        "operation": command.operation.value,
        "source_id": str(command.source_id),
        "event_id": str(command.event_id),
        "policy_revision": decision.policy_revision_number,
        "policy_outcome": decision.outcome.value,
        "expected_version_id": str(command.expected_version_id),
    }
    if result is not None:
        envelope["source_version_id"] = str(result.source_version_id)
        envelope["event_sequence"] = result.event_sequence
        envelope["tombstone_id"] = None if result.tombstone_id is None else str(result.tombstone_id)
    canonical = _canonical_json_bytes(envelope)
    return hashlib.sha256(canonical).hexdigest()


def _canonical_json_bytes(mapping: Mapping[str, object]) -> bytes:
    import json

    return json.dumps(
        mapping,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


# --- actor derivation -------------------------------------------------------


def _actor_for_device_context(
    device_context: LifecycleDeviceContext,
) -> SourceActor:
    from personal_os.sources.actors import ActorKind

    return SourceActor(ActorKind.DEVICE, device_context.device_id)


# --- store implementation -----------------------------------------------------


class PostgresqlSourceLifecycleStore:
    """Atomic lifecycle transition adapter over the canonical baseline.

    The store takes the composition-owned :class:`AsyncEngine`, the
    injectable policy verifier, optional rejection diagnostic sink and
    metrics, and an injectable UUIDv7 allocator seam. It opens no
    connection at construction; every method runs one
    ``READ COMMITTED`` transaction behind the pinned ``SET LOCAL``
    bounds, and every commit returns the canonical
    :class:`SourceLifecycleCommitResult` so the service can hydrate the
    API response and the dispatched projection intents from the same
    frozen evidence.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        policy_verifier: PolicyTrustAnchorVerifier,
        diagnostics: Any | None = None,
        metrics: Any | None = None,
        retry: DatabaseRetryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        identity_generator: Callable[[], UUID] | None = None,
    ) -> None:
        self._engine = engine
        self._policy_verifier = policy_verifier
        self._diagnostics = diagnostics
        self._metrics = metrics
        self._retry = retry if retry is not None else DatabaseRetryPolicy()
        self._clock = clock if clock is not None else _default_clock
        self._identity_generator = identity_generator if identity_generator is not None else uuid7

    # -- replay -----------------------------------------------------------

    async def resolve_committed(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> SourceLifecycleCommitResult | None:
        """Lock-free exact-replay lookup keyed by workspace, event and fingerprint.

        The lookup is keyed by ``(workspace_id, idempotency_key)`` first
        and ``event_id`` second; the second lookup only runs when the
        first misses, so the index-only preflight stays on the unique
        constraint. An exact fingerprint returns the canonical committed
        result; any drift rejects with the closed error vocabulary and
        records no audit (the rejection is a no-network preflight).
        """

        del diagnostic_context
        if request_fingerprint.hexadecimal != fingerprint_lifecycle_command(command).hexadecimal:
            return None
        return await self._retry.run(
            lambda _attempt: self._resolve_committed_once(command, device_context),
            source_id=command.source_id,
        )

    async def _resolve_committed_once(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
    ) -> SourceLifecycleCommitResult | None:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            if not await self._workspace_is_trusted(connection, device_context):
                raise SourceLifecycleError(SourceLifecycleErrorCode.INPUT_INVALID)
            key_row = await self._fetch_replay_row(
                connection,
                sync_event_lookup_by_key_statement(
                    device_context.workspace_id, command.idempotency_key
                ),
            )
            if key_row is None:
                return None
            if (
                key_row.workspace_id != device_context.workspace_id
                or key_row.source_id != command.source_id
            ):
                raise SourceLifecycleError(SourceLifecycleErrorCode.INPUT_INVALID)
            if key_row.request_fingerprint != fingerprint_lifecycle_command(command).hexadecimal:
                raise SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT)
            if key_row.event_id != command.event_id:
                raise SourceLifecycleError(SourceLifecycleErrorCode.INPUT_INVALID)
            return _hydrate_replay_result(key_row, command)

    async def _fetch_replay_row(
        self, connection: AsyncConnection, statement: sa.Select[tuple[Any, ...]]
    ) -> LifecycleReplayLookupRow | None:
        result = await connection.execute(statement)
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return LifecycleReplayLookupRow.from_result_row(row)

    # -- commit -----------------------------------------------------------

    async def commit(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
    ) -> SourceLifecycleCommitResult:
        """Atomic lifecycle commit behind the locked prefix and retry policy.

        The transaction follows the spec 9.1 order: replay lookup,
        idempotency identity lock, source advisory + row lock, locator
        advisory locks in canonical text order with their rows, optional
        tombstone row lock, then validation and writes. Business and
        integrity failures raise the closed vocabulary and never retry;
        connection-class unavailability triggers a bounded three-attempt
        retry and an ambiguous-commit evidence lookup.
        """

        if request_fingerprint.hexadecimal != fingerprint_lifecycle_command(command).hexadecimal:
            raise SourceLifecycleError(SourceLifecycleErrorCode.INPUT_INVALID)
        try:
            return await self._retry.run(
                lambda _attempt: self._commit_lifecycle_once(
                    command,
                    device_context,
                    request_fingerprint,
                    policy_decision,
                    diagnostic_context,
                ),
                source_id=command.source_id,
                recover=lambda: self._recover_committed(
                    command, device_context, request_fingerprint
                ),
            )
        except SourceLifecycleError as error:
            if error.code is SourceLifecycleErrorCode.LOCATOR_CONFLICT:
                await self._insert_rejection_audit(
                    command=command,
                    device_context=device_context,
                    policy_decision=policy_decision,
                    diagnostic_context=diagnostic_context,
                    reason_code=REASON_IDEMPOTENCY_MISMATCH,
                )
            raise

    async def _commit_lifecycle_once(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
    ) -> SourceLifecycleCommitResult:
        # Spec 9.1 step 1: replay lookup before acquiring any locks. An exact
        # fingerprint replay returns the canonical committed result without
        # mutation — the second ``commit`` call is a no-op that produces zero
        # new rows.
        replayed = await self.resolve_committed(
            command=command,
            device_context=device_context,
            request_fingerprint=request_fingerprint,
            diagnostic_context=diagnostic_context,
        )
        if replayed is not None:
            return replayed
        identities = LifecycleCommitIdentities.allocate(
            include_tombstone=command.operation is LifecycleOperation.DELETE,
            include_locator=command.operation is not LifecycleOperation.DELETE,
        )
        started_monotonic = _monotonic()
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            if not await self._workspace_is_trusted(connection, device_context):
                raise SourceLifecycleError(SourceLifecycleErrorCode.INPUT_INVALID)
            await connection.execute(
                idempotency_lock_statement(
                    device_context.workspace_id,
                    _wrap_idempotency_key(command.idempotency_key),
                )
            )
            # Source advisory lock + row lock (spec 9.1 step 2).
            await connection.execute(source_lock_statement(command.source_id))
            source_state, current_version_id, active_locator = await self._lock_source_row(
                connection, command.source_id
            )
            await self._lock_locator_keys(connection, command, active_locator)
            tombstone_row = await self._maybe_lock_tombstone(connection, command, device_context)
            # Validate the precondition graph before any writes.
            self._validate_preconditions(
                command=command,
                device_context=device_context,
                source_state=source_state,
                current_version_id=current_version_id,
                active_locator=active_locator,
                tombstone_row=tombstone_row,
            )
            # Re-evaluate the locked policy under the authoritative revision.
            await self._evaluate_locked_policy(
                connection=connection,
                command=command,
                device_context=device_context,
                policy_decision=policy_decision,
                active_locator=active_locator,
            )
            # Acquire target locator row lock if needed and confirm availability.
            await self._lock_target_locator_row(connection, command, device_context)
            # Atomic transition writes.
            result = await self._commit_transition(
                connection=connection,
                command=command,
                device_context=device_context,
                policy_decision=policy_decision,
                diagnostic_context=diagnostic_context,
                identities=identities,
                source_state=source_state,
                current_version_id=current_version_id,
                active_locator=active_locator,
                tombstone_row=tombstone_row,
            )
        duration = _monotonic() - started_monotonic
        if self._metrics is not None:
            from personal_os.source_lifecycle.metrics import LifecycleMetricOutcome

            self._metrics.record_commit(
                operation=command.operation,
                outcome=LifecycleMetricOutcome.COMMITTED,
                duration_seconds=duration,
            )
        return result

    # --- preconditions ---------------------------------------------------

    async def _workspace_is_trusted(
        self,
        connection: AsyncConnection,
        device_context: LifecycleDeviceContext,
    ) -> bool:
        from postgresql_source_store.tables import devices, workspaces

        workspace_result = await connection.execute(
            sa.select(workspaces.c.status).where(
                workspaces.c.workspace_id == device_context.workspace_id
            )
        )
        if str(workspace_result.scalar_one_or_none() or "") != "active":
            return False
        device_result = await connection.execute(
            sa.select(devices.c.status).where(
                devices.c.workspace_id == device_context.workspace_id,
                devices.c.device_id == device_context.device_id,
            )
        )
        return str(device_result.scalar_one_or_none() or "") == "active"

    async def _lock_source_row(
        self, connection: AsyncConnection, source_id: UUID
    ) -> tuple[str | None, UUID | None, NormalizedLocator | None]:
        from postgresql_source_store.tables import sources as sources_table

        result = await connection.execute(
            sa.select(
                sources_table.c.sync_state,
                sources_table.c.current_version_id,
                source_locators.c.normalized_locator,
            )
            .select_from(sources_table)
            .outerjoin(
                source_locators,
                sa.and_(
                    source_locators.c.source_id == sources_table.c.source_id,
                    source_locators.c.closed_event_id.is_(None),
                ),
            )
            .where(sources_table.c.source_id == source_id)
            .with_for_update(of=sources_table)
        )
        row = result.one_or_none()
        if row is None:
            return None, None, None
        locator_value = row.normalized_locator
        active_locator: NormalizedLocator | None = None
        if isinstance(locator_value, str) and locator_value:
            active_locator = NormalizedLocator(locator_value)
        return str(row.sync_state), row.current_version_id, active_locator

    async def _lock_locator_keys(
        self,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        active_locator: NormalizedLocator | None,
    ) -> None:
        locators: list[NormalizedLocator] = []
        if active_locator is not None:
            locators.append(active_locator)
        if command.operation in {
            LifecycleOperation.RENAME,
            LifecycleOperation.MOVE,
            LifecycleOperation.RESTORE,
        }:
            target = command.target_locator
            if target is not None and (
                active_locator is None or target.value != active_locator.value
            ):
                locators.append(target)
        for locator, _key in order_locator_lock_keys(locators):
            await connection.execute(locator_advisory_lock_statement(locator))

    async def _maybe_lock_tombstone(
        self,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
    ) -> dict[str, Any] | None:
        if command.operation is not LifecycleOperation.RESTORE:
            return None
        if command.tombstone_id is None:
            return None
        result = await connection.execute(
            tombstone_lookup_by_id_statement(
                workspace_id=device_context.workspace_id,
                source_tombstone_id=command.tombstone_id,
            ).with_for_update()
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return dict(row)

    async def _lock_target_locator_row(
        self,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
    ) -> None:
        if command.operation not in {
            LifecycleOperation.RENAME,
            LifecycleOperation.MOVE,
            LifecycleOperation.RESTORE,
        }:
            return
        if command.target_locator is None:
            return
        # The advisory lock is already taken. Confirm the row is not held by
        # another active source.
        from postgresql_source_store.tables import sources as sources_table

        result = await connection.execute(
            sa.select(sources_table.c.source_id)
            .select_from(source_locators)
            .outerjoin(
                sources_table,
                sa.and_(
                    sources_table.c.source_id == source_locators.c.source_id,
                    sources_table.c.sync_state == SOURCE_STATE_ACTIVE,
                ),
            )
            .where(
                source_locators.c.workspace_id == device_context.workspace_id,
                source_locators.c.normalized_locator == command.target_locator.value,
                source_locators.c.closed_event_id.is_(None),
            )
            .with_for_update(of=source_locators)
        )
        holder_row = result.first()
        if holder_row is None:
            return
        holder_source_id = holder_row[0]
        if holder_source_id is None or holder_source_id == command.source_id:
            return
        raise SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT)

    def _validate_preconditions(
        self,
        *,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        source_state: str | None,
        current_version_id: UUID | None,
        active_locator: NormalizedLocator | None,
        tombstone_row: dict[str, Any] | None,
    ) -> None:
        if source_state is None:
            raise SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_MISSING)
        state_error = classify_state_mismatch(
            actual_state=source_state, operation=command.operation
        )
        if state_error is not None:
            raise state_error
        version_error = classify_version_mismatch(
            expected=command.expected_version_id,
            actual=current_version_id if current_version_id is not None else UUID(int=0),
        )
        if version_error is not None:
            raise version_error
        if command.operation in {
            LifecycleOperation.RENAME,
            LifecycleOperation.MOVE,
            LifecycleOperation.DELETE,
        }:
            if command.expected_locator is None:
                raise SourceLifecycleError(SourceLifecycleErrorCode.INPUT_INVALID)
            locator_error = classify_locator_conflict(
                expected=command.expected_locator,
                actual=active_locator,
            )
            if locator_error is not None:
                raise locator_error
        if command.operation in {
            LifecycleOperation.RENAME,
            LifecycleOperation.MOVE,
        }:
            if command.target_locator is None:
                raise SourceLifecycleError(SourceLifecycleErrorCode.INPUT_INVALID)
            classification_error = classify_classification(
                operation=command.operation,
                expected_locator=command.expected_locator
                if command.expected_locator is not None
                else active_locator or NormalizedLocator("."),
                target_locator=command.target_locator,
            )
            if classification_error is not None:
                raise classification_error
        if command.operation is LifecycleOperation.RESTORE:
            tombstone_error = classify_tombstone_conflict(
                tombstone_present=tombstone_row is not None,
                tombstone_already_restored=bool(
                    tombstone_row is not None and tombstone_row.get("restore_event_id") is not None
                ),
                tombstone_id=command.tombstone_id or UUID(int=0),
            )
            if tombstone_error is not None:
                raise tombstone_error
            if tombstone_row is not None and tombstone_row.get("source_id") != command.source_id:
                raise SourceLifecycleError(SourceLifecycleErrorCode.TOMBSTONE_NOT_FOUND)
            if tombstone_row is not None:
                retained_version_id = tombstone_row.get("retained_version_id")
                if (
                    isinstance(retained_version_id, UUID)
                    and retained_version_id != current_version_id
                ):
                    raise SourceLifecycleError(SourceLifecycleErrorCode.VERSION_CONFLICT)
        if command.tombstone_id is not None and command.operation is not LifecycleOperation.RESTORE:
            raise SourceLifecycleError(SourceLifecycleErrorCode.INPUT_INVALID)
        del device_context

    async def _evaluate_locked_policy(
        self,
        *,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        policy_decision: LifecyclePolicyDecision,
        active_locator: NormalizedLocator | None,
    ) -> None:
        # Load the locked policy-state row so the authoritative revision
        # is captured even on the unchanged-revision fast path. Per the
        # spec, the externally passed verdict is the only authoritative
        # signal for ``_projection_intent_operation_for`` (delete vs
        # upsert) — denied or indeterminate rename / move / restore
        # still commit the truthful canonical locator state. The locked
        # re-evaluation runs only on revision mismatch; the verdict it
        # computes is discarded (see ``del decision`` below) and never
        # flows back to the caller, so it does NOT influence intent
        # operation selection.
        material = await load_locked_active_policy_snapshot(connection, device_context.workspace_id)
        revision = parse_verified_policy_revision(material, verifier=self._policy_verifier)
        # Fast path: the locked revision matches the externally passed one.
        # The externally passed verdict is trusted for intent operation
        # selection; no rejection on DENIED/INDETERMINATE per spec.
        if (
            revision.revision_number == policy_decision.policy_revision_number
            and policy_decision.workspace_id == device_context.workspace_id
        ):
            del material, revision
            return
        # Slow path: re-evaluate under the locked policy on revision mismatch.
        # The locked verdict is computed for parity/observability only;
        # the transition commits regardless of outcome. The verdict is
        # discarded after evaluation — intent operation selection is
        # driven exclusively by the externally passed ``policy_decision``
        # in ``_projection_intent_operation_for``.
        locator_value: str | None
        if command.target_locator is not None:
            locator_value = command.target_locator.value
        elif active_locator is not None:
            locator_value = active_locator.value
        else:
            locator_value = None
        subject = PolicySubject(
            workspace_id=device_context.workspace_id,
            source_id=command.source_id,
            normalized_locator=locator_value,
        )
        decision = evaluate_policy_decision(
            revision=revision,
            subject=subject,
            evaluated_at=self._clock(),
        )
        # Bound unused names. The locked verdict is computed but not
        # retained — the externally passed ``policy_decision`` drives
        # intent operation selection.
        del subject
        del decision
        del material
        del revision

    # --- atomic transition ----------------------------------------------

    async def _commit_transition(
        self,
        *,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
        identities: LifecycleCommitIdentities,
        source_state: str | None,
        current_version_id: UUID | None,
        active_locator: NormalizedLocator | None,
        tombstone_row: dict[str, Any] | None,
    ) -> SourceLifecycleCommitResult:
        del tombstone_row, source_state
        operation = command.operation
        if current_version_id is None:
            raise SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_MISSING)
        if operation is LifecycleOperation.DELETE:
            return await self._commit_delete(
                connection=connection,
                command=command,
                device_context=device_context,
                policy_decision=policy_decision,
                diagnostic_context=diagnostic_context,
                identities=identities,
                current_version_id=current_version_id,
                active_locator=active_locator,
            )
        if operation is LifecycleOperation.RESTORE:
            return await self._commit_restore(
                connection=connection,
                command=command,
                device_context=device_context,
                policy_decision=policy_decision,
                diagnostic_context=diagnostic_context,
                identities=identities,
                current_version_id=current_version_id,
            )
        return await self._commit_rename_or_move(
            connection=connection,
            command=command,
            device_context=device_context,
            policy_decision=policy_decision,
            diagnostic_context=diagnostic_context,
            identities=identities,
            current_version_id=current_version_id,
            active_locator=active_locator,
        )

    async def _commit_rename_or_move(
        self,
        *,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
        identities: LifecycleCommitIdentities,
        current_version_id: UUID,
        active_locator: NormalizedLocator | None,
    ) -> SourceLifecycleCommitResult:
        target = command.target_locator
        if target is None:
            raise SourceLifecycleError(SourceLifecycleErrorCode.INPUT_INVALID)
        # Insert the lifecycle event first so the canonical sequence is known
        # before any locator write. This is the single sync_events insert for
        # the transition (spec 9.1) — closing/opening locator rows use the
        # sourced sequence directly, so no back-update is required.
        event_sequence, committed_at = await self._insert_lifecycle_event(
            connection=connection,
            command=command,
            device_context=device_context,
            current_version_id=current_version_id,
            event_sequence=0,
            committed_at=None,
        )
        if active_locator is not None:
            await self._close_existing_locator(
                connection=connection,
                command=command,
                device_context=device_context,
                active_locator=active_locator,
                closed_sequence=event_sequence,
            )
        opened = await connection.execute(
            locator_open_insert_statement(
                source_locator_id=identities.source_locator_id,
                workspace_id=device_context.workspace_id,
                source_id=command.source_id,
                locator=target,
                opened_event_id=command.event_id,
                opened_sequence=event_sequence,
            )
        )
        if opened.scalar_one_or_none() != identities.source_locator_id:
            raise SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT)
        if command.operation is LifecycleOperation.RENAME:
            await self._update_rename_title(
                connection=connection,
                command=command,
                target=target,
            )
        intent_operation = _projection_intent_operation_for(command, policy_decision)
        await self._insert_lifecycle_intents(
            connection=connection,
            command=command,
            device_context=device_context,
            identities=identities,
            current_version_id=current_version_id,
            operation=intent_operation,
        )
        await self._insert_lifecycle_audit(
            connection=connection,
            command=command,
            device_context=device_context,
            policy_decision=policy_decision,
            diagnostic_context=diagnostic_context,
            identities=identities,
            resulting_locator=target,
            tombstone_id=None,
            event_sequence=event_sequence,
            committed_at=committed_at,
            state=LifecycleState.ACTIVE,
        )
        return SourceLifecycleCommitResult(
            source_id=command.source_id,
            source_version_id=current_version_id,
            event_id=command.event_id,
            event_sequence=event_sequence,
            state=LifecycleState.ACTIVE,
            tombstone_id=None,
            resulting_locator=target,
            committed_at=committed_at,
        )

    async def _commit_delete(
        self,
        *,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
        identities: LifecycleCommitIdentities,
        current_version_id: UUID,
        active_locator: NormalizedLocator | None,
    ) -> SourceLifecycleCommitResult:
        if command.expected_locator is None:
            raise SourceLifecycleError(SourceLifecycleErrorCode.INPUT_INVALID)
        # Insert the lifecycle event first so the canonical sequence is known
        # before any locator or tombstone write. This is the single sync_events
        # insert for the transition (spec 9.1); the locator closure uses the
        # sourced sequence directly so no back-update is required.
        event_sequence, committed_at = await self._insert_lifecycle_event(
            connection=connection,
            command=command,
            device_context=device_context,
            current_version_id=current_version_id,
            event_sequence=0,
            committed_at=None,
        )
        await self._close_existing_locator(
            connection=connection,
            command=command,
            device_context=device_context,
            active_locator=active_locator
            if active_locator is not None
            else command.expected_locator,
            closed_sequence=event_sequence,
        )
        guarded = await connection.execute(close_tombstone_set_delete(command.source_id))
        if guarded.rowcount != 1:
            raise SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_MISSING)
        tombstone_id = identities.tombstone_id
        if tombstone_id is None:
            raise SourceLifecycleError(SourceLifecycleErrorCode.COMMIT_OUTCOME_UNKNOWN)
        await self._insert_tombstone(
            connection=connection,
            command=command,
            device_context=device_context,
            identities=identities,
            current_version_id=current_version_id,
            retained_locator=active_locator or command.expected_locator,
            tombstone_id=tombstone_id,
        )
        intent_operation = _projection_intent_operation_for(command, policy_decision)
        await self._insert_lifecycle_intents(
            connection=connection,
            command=command,
            device_context=device_context,
            identities=identities,
            current_version_id=current_version_id,
            operation=intent_operation,
        )
        await self._insert_lifecycle_audit(
            connection=connection,
            command=command,
            device_context=device_context,
            policy_decision=policy_decision,
            diagnostic_context=diagnostic_context,
            identities=identities,
            resulting_locator=None,
            tombstone_id=tombstone_id,
            event_sequence=event_sequence,
            committed_at=committed_at,
            state=LifecycleState.DELETED,
        )
        return SourceLifecycleCommitResult(
            source_id=command.source_id,
            source_version_id=current_version_id,
            event_id=command.event_id,
            event_sequence=event_sequence,
            state=LifecycleState.DELETED,
            tombstone_id=tombstone_id,
            resulting_locator=None,
            committed_at=committed_at,
        )

    async def _commit_restore(
        self,
        *,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
        identities: LifecycleCommitIdentities,
        current_version_id: UUID,
    ) -> SourceLifecycleCommitResult:
        target = command.target_locator
        if target is None or command.tombstone_id is None:
            raise SourceLifecycleError(SourceLifecycleErrorCode.INPUT_INVALID)
        # The tombstone's restore-event foreign key requires its canonical event
        # to exist before the tombstone can be closed.
        event_sequence, committed_at = await self._insert_lifecycle_event(
            connection=connection,
            command=command,
            device_context=device_context,
            current_version_id=current_version_id,
            event_sequence=0,
            committed_at=None,
        )
        tombstone_close = await connection.execute(
            tombstone_close_statement(
                source_tombstone_id=command.tombstone_id,
                restore_event_id=command.event_id,
            )
        )
        if tombstone_close.rowcount != 1:
            raise SourceLifecycleError(SourceLifecycleErrorCode.TOMBSTONE_CLOSED)
        guarded = await connection.execute(restore_source_update_statement(command.source_id))
        if guarded.rowcount != 1:
            raise SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_MISSING)
        opened = await connection.execute(
            locator_open_insert_statement(
                source_locator_id=identities.source_locator_id,
                workspace_id=device_context.workspace_id,
                source_id=command.source_id,
                locator=target,
                opened_event_id=command.event_id,
                opened_sequence=event_sequence,
            )
        )
        if opened.scalar_one_or_none() != identities.source_locator_id:
            raise SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT)
        intent_operation = _projection_intent_operation_for(command, policy_decision)
        await self._insert_lifecycle_intents(
            connection=connection,
            command=command,
            device_context=device_context,
            identities=identities,
            current_version_id=current_version_id,
            operation=intent_operation,
        )
        await self._insert_lifecycle_audit(
            connection=connection,
            command=command,
            device_context=device_context,
            policy_decision=policy_decision,
            diagnostic_context=diagnostic_context,
            identities=identities,
            resulting_locator=target,
            tombstone_id=None,
            event_sequence=event_sequence,
            committed_at=committed_at,
            state=LifecycleState.ACTIVE,
        )
        return SourceLifecycleCommitResult(
            source_id=command.source_id,
            source_version_id=current_version_id,
            event_id=command.event_id,
            event_sequence=event_sequence,
            state=LifecycleState.ACTIVE,
            tombstone_id=None,
            resulting_locator=target,
            committed_at=committed_at,
        )

    # --- write boundaries -----------------------------------------------

    async def _close_existing_locator(
        self,
        *,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        active_locator: NormalizedLocator | None,
        closed_sequence: int,
    ) -> None:
        del device_context
        if active_locator is None:
            return
        result = await connection.execute(
            sa.select(source_locators.c.source_locator_id)
            .where(
                source_locators.c.source_id == command.source_id,
                source_locators.c.normalized_locator == active_locator.value,
                source_locators.c.closed_event_id.is_(None),
            )
            .with_for_update()
        )
        row = result.first()
        if row is None:
            return
        source_locator_id = row[0]
        # The lifecycle event insert sources the canonical sequence before
        # this closure runs, so the locator row is closed with the real
        # sequence directly — no placeholder + back-update is needed.
        close = await connection.execute(
            close_locator_statement(
                source_locator_id=source_locator_id,
                closed_event_id=command.event_id,
                closed_sequence=closed_sequence,
            )
        )
        if close.rowcount != 1:
            raise SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT)

    async def _update_rename_title(
        self,
        *,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        target: NormalizedLocator,
    ) -> None:
        title = derive_title_v1(target)
        result = await connection.execute(
            rename_title_update_statement(source_id=command.source_id, title_value=title.value)
        )
        if result.rowcount != 1:
            raise SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT)

    async def _insert_tombstone(
        self,
        *,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        identities: LifecycleCommitIdentities,
        current_version_id: UUID,
        retained_locator: NormalizedLocator,
        tombstone_id: UUID,
    ) -> None:
        await connection.execute(
            open_tombstone_insert_statement(
                source_tombstone_id=tombstone_id,
                workspace_id=device_context.workspace_id,
                source_id=command.source_id,
                delete_event_id=command.event_id,
                retained_version_id=current_version_id,
                retained_locator=retained_locator,
                actor_kind="device",
                actor_id=device_context.device_id,
            )
        )

    async def _insert_lifecycle_event(
        self,
        *,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        current_version_id: UUID,
        event_sequence: int,
        committed_at: datetime | None,
    ) -> tuple[int, datetime]:
        result = await connection.execute(
            event_insert_statement(
                event_id=command.event_id,
                workspace_id=device_context.workspace_id,
                source_id=command.source_id,
                device_id=device_context.device_id,
                committed_version_id=current_version_id,
                base_version_id=command.expected_version_id,
                idempotency_key=command.idempotency_key,
                request_fingerprint=fingerprint_lifecycle_command(command).hexadecimal,
                event_type=EVENT_TYPE_BY_OPERATION[command.operation],
                client_timestamp=command.client_timestamp,
            )
        )
        row = result.one()
        return int(row.event_sequence), row.committed_at

    async def _insert_lifecycle_intents(
        self,
        *,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        identities: LifecycleCommitIdentities,
        current_version_id: UUID,
        operation: str,
    ) -> None:
        for intent_id, projection_kind in (
            (identities.qdrant_intent_id, PROJECTION_KIND_QDRANT),
            (identities.neo4j_intent_id, PROJECTION_KIND_NEO4J),
        ):
            await connection.execute(
                intent_insert_statement(
                    projection_intent_id=intent_id,
                    workspace_id=device_context.workspace_id,
                    event_id=command.event_id,
                    source_id=command.source_id,
                    source_version_id=current_version_id,
                    projection_kind=projection_kind,
                    operation=operation,
                )
            )

    async def _insert_lifecycle_audit(
        self,
        *,
        connection: AsyncConnection,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
        identities: LifecycleCommitIdentities,
        resulting_locator: NormalizedLocator | None,
        tombstone_id: UUID | None,
        event_sequence: int,
        committed_at: datetime,
        state: LifecycleState,
    ) -> None:
        safe_diff = _compute_safe_diff_digest(
            command=command,
            decision=policy_decision,
            result=SourceLifecycleCommitResult(
                source_id=command.source_id,
                source_version_id=command.expected_version_id,
                event_id=command.event_id,
                event_sequence=event_sequence,
                state=state,
                tombstone_id=tombstone_id,
                resulting_locator=resulting_locator,
                committed_at=committed_at,
            ),
        )
        await connection.execute(
            audit_insert_statement(
                audit_event_id=identities.audit_event_id,
                workspace_id=device_context.workspace_id,
                actor_kind="device",
                actor_id=device_context.device_id,
                action=AUDIT_ACTIONS_BY_OPERATION[command.operation],
                target_kind=AUDIT_TARGET_KIND_SOURCE,
                target_id=command.source_id,
                request_id=diagnostic_context.request_id,
                client_request_id=diagnostic_context.client_request_id,
                trace_id=diagnostic_context.trace.trace_id.value,
                result="succeeded",
                reason_code=None,
                safe_diff_hash=safe_diff,
            )
        )

    async def _insert_rejection_audit(
        self,
        *,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
        reason_code: str,
    ) -> None:
        """Persist the redacted audit evidence for a closed rejection."""

        async with self._engine.begin() as connection:
            await connection.execute(
                audit_insert_statement(
                    audit_event_id=self._identity_generator(),
                    workspace_id=device_context.workspace_id,
                    actor_kind="device",
                    actor_id=device_context.device_id,
                    action=f"{AUDIT_ACTIONS_BY_OPERATION[command.operation]}.rejected",
                    target_kind=AUDIT_TARGET_KIND_SOURCE,
                    target_id=command.source_id,
                    request_id=diagnostic_context.request_id,
                    client_request_id=diagnostic_context.client_request_id,
                    trace_id=diagnostic_context.trace.trace_id.value,
                    result="rejected",
                    reason_code=reason_code,
                    safe_diff_hash=_compute_safe_diff_digest(
                        command=command,
                        decision=policy_decision,
                        result=None,
                    ),
                )
            )

    # --- recovery --------------------------------------------------------

    async def _recover_committed(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
    ) -> SourceLifecycleCommitResult | None:
        """Run a fresh-connection evidence lookup for an ambiguous commit.

        A connection-class failure during the commit window proves
        nothing about whether the canonical graph landed; only a fresh
        evidence lookup can decide. The lookup mirrors
        :meth:`resolve_committed` so an identical replay returns the
        canonical result. ``None`` is a proven absence (no retry attempt
        wins on this evidence).
        """

        return await self.resolve_committed(
            command=command,
            device_context=device_context,
            request_fingerprint=request_fingerprint,
            diagnostic_context=_empty_diagnostic_context(),
        )


def _hydrate_replay_result(
    row: LifecycleReplayLookupRow, command: SourceLifecycleCommand
) -> SourceLifecycleCommitResult:
    """Build the canonical commit result from one replay lookup row.

    The ``resulting_locator_value`` column is the normalized locator
    text the lifecycle event opened (for rename/move / / restore) or
    ``None`` for delete; the ``tombstone_id`` column is the tombstone
    identity for delete and ``None`` otherwise.
    """

    if row.event_type not in {"rename", "move", "delete", "restore"}:
        raise SourceLifecycleError(SourceLifecycleErrorCode.COMMIT_OUTCOME_UNKNOWN)
    if row.committed_version_id is None:
        raise SourceLifecycleError(SourceLifecycleErrorCode.COMMIT_OUTCOME_UNKNOWN)
    if row.event_type == "delete":
        if row.tombstone_id is None:
            raise SourceLifecycleError(SourceLifecycleErrorCode.COMMIT_OUTCOME_UNKNOWN)
        return SourceLifecycleCommitResult(
            source_id=command.source_id,
            source_version_id=row.committed_version_id,
            event_id=row.event_id,
            event_sequence=row.event_sequence,
            state=LifecycleState.DELETED,
            tombstone_id=row.tombstone_id,
            resulting_locator=None,
            committed_at=row.committed_at,
        )
    if row.resulting_locator_value is None:
        raise SourceLifecycleError(SourceLifecycleErrorCode.COMMIT_OUTCOME_UNKNOWN)
    resulting_locator = NormalizedLocator(row.resulting_locator_value)
    return SourceLifecycleCommitResult(
        source_id=command.source_id,
        source_version_id=row.committed_version_id,
        event_id=row.event_id,
        event_sequence=row.event_sequence,
        state=LifecycleState.ACTIVE,
        tombstone_id=None,
        resulting_locator=resulting_locator,
        committed_at=row.committed_at,
    )


def _empty_diagnostic_context() -> DiagnosticContext:
    return create_diagnostic_context_for_recovery()


def create_diagnostic_context_for_recovery() -> DiagnosticContext:
    from personal_os.diagnostics.trace_context import SpanId, TraceContext, TraceId

    return DiagnosticContext(
        request_id=uuid7(),
        client_request_id=None,
        trace=TraceContext(
            trace_id=TraceId("0af7651916cd43dd8448eb211c80319c"),
            remote_parent_span_id=None,
            local_span_id=SpanId("b7ad6b7169203331"),
            trace_flags=0,
        ),
    )


def _default_clock() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


def _monotonic() -> float:
    import time

    return time.monotonic()


# --- exports -----------------------------------------------------------------


__all__ = [
    "AUDIT_ACTIONS_BY_OPERATION",
    "AUDIT_TARGET_KIND_SOURCE",
    "EVENT_TYPE_BY_OPERATION",
    "PROJECTION_KIND_NEO4J",
    "PROJECTION_KIND_QDRANT",
    "PROJECTION_OPERATION_DELETE",
    "PROJECTION_OPERATION_UPSERT",
    "REASON_LOCATOR_CONFLICT",
    "REASON_LOCATOR_MISSING",
    "REASON_TOMBSTONE_CLOSED",
    "REASON_TOMBSTONE_NOT_FOUND",
    "REASON_VERSION_CONFLICT",
    "SOURCE_STATE_ACTIVE",
    "SOURCE_STATE_DELETED",
    "LifecycleCommitIdentities",
    "LifecycleReplayLookupRow",
    "PostgresqlSourceLifecycleStore",
    "advisory_lock_key_for_locator",
    "audit_insert_statement",
    "classify_classification",
    "classify_locator_conflict",
    "classify_state_mismatch",
    "classify_target_availability",
    "classify_tombstone_conflict",
    "classify_version_mismatch",
    "close_locator_statement",
    "close_tombstone_set_delete",
    "event_insert_statement",
    "intent_insert_statement",
    "is_locator_lock_order_valid",
    "locator_advisory_lock_statement",
    "locator_open_insert_statement",
    "open_tombstone_insert_statement",
    "order_locator_lock_keys",
    "rename_title_update_statement",
    "restore_source_update_statement",
    "source_lock_statement",
    "sync_event_lookup_by_event_statement",
    "sync_event_lookup_by_key_statement",
    "tombstone_close_statement",
    "tombstone_lookup_by_id_statement",
]
