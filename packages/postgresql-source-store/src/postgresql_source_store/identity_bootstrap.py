"""Atomic PostgreSQL identity bootstrap store (design spec 5.3, 5.4).

:class:`PostgresqlIdentityBootstrapStore` implements the durable
:class:`~personal_os.identity.ports.IdentityBootstrapStore` port over the
canonical baseline. ``bootstrap`` runs one ``READ COMMITTED`` transaction:
the pinned ``SET LOCAL`` bounds, a transaction-scoped bootstrap advisory lock
in a reserved namespace derived from the exact ``username:workspace_key``
material, then a read of the user/workspace/device state. An empty state
creates the active user, workspace and bootstrap device plus the succeeded
``identity.bootstrap_completed`` audit row — all four rows sharing the single
``SELECT now()`` transaction timestamp and committed once, so a fault after
any insert rolls back everything. Existing state is classified without
mutation: an exact replay returns the originally committed ids with the
stored workspace creation timestamp and writes no extra audit row; a drift
conflict rolls back first, then — only when a trusted canonical workspace can
be established — writes one standalone ``identity.bootstrap_rejected`` audit
row in its own short transaction, otherwise emits only the registered
``identity_bootstrap_rejected`` diagnostic event. The rejected values
themselves never reach an audit row, diagnostic field or error detail.

Driver failures are routed through
:func:`postgresql_source_store.error_mapping.map_database_failure` with a nil
internal UUID sentinel, so SQLSTATE, SQL, parameters and driver text never
leave the adapter.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy import TextClause
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import EventName
from personal_os.error_contracts.codes import ErrorCode
from personal_os.identity.bootstrap import (
    ExistingIdentityDevice,
    ExistingIdentityState,
    ExistingIdentityUser,
    ExistingIdentityWorkspace,
    classify_existing_identity,
    resolve_trusted_workspace_id,
)
from personal_os.identity.contracts import (
    BootstrapIdentityCommand,
    BootstrapIdentityOutcome,
    BootstrapIdentityResult,
    IdentityBootstrapError,
)
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import map_database_failure
from postgresql_source_store.locks import (
    advisory_xact_lock_statement,
    signed_first_sha256_word,
)
from postgresql_source_store.tables import audit_events, devices, users, workspaces

#: Reserved advisory-lock namespace for identity bootstrap (distinct from
#: the idempotency and source namespaces; ``"SVCB"`` in the established scheme).
IDENTITY_BOOTSTRAP_LOCK_NAMESPACE: Final[int] = 0x53564342

IDENTITY_BOOTSTRAP_AUDIT_ACTION: Final[str] = "identity.bootstrap_completed"
IDENTITY_REJECTION_AUDIT_ACTION: Final[str] = "identity.bootstrap_rejected"
IDENTITY_REJECTION_REASON: Final[str] = "identity_state_conflict"

#: Audit-row literals shared by the completed and rejected bootstrap actions.
IDENTITY_AUDIT_ACTOR_KIND_SYSTEM: Final[str] = "system"
IDENTITY_AUDIT_TARGET_KIND_WORKSPACE: Final[str] = "workspace"
IDENTITY_AUDIT_RESULT_SUCCEEDED: Final[str] = "succeeded"
IDENTITY_AUDIT_RESULT_REJECTED: Final[str] = "rejected"

#: Identity lifecycle states written and required by the bootstrap transitions.
_USER_STATUS_ACTIVE: Final[str] = "active"
_WORKSPACE_STATUS_ACTIVE: Final[str] = "active"
_DEVICE_STATUS_ACTIVE: Final[str] = "active"

#: Internal sentinel for the non-source failure mapping: this store owns no
#: source identity, so the safe detail carries only the nil UUID and stays
#: inside the adapter boundary.
_NIL_SOURCE_ID: Final[UUID] = UUID(int=0)

#: Mapping lock-key material separator cannot appear in validated bootstrap
#: keys (``^[a-z0-9][a-z0-9._-]{0,63}$``), keeping the material unambiguous.
_LOCK_KEY_SEPARATOR: Final[str] = ":"

#: One row of an identity-state read: a SQLAlchemy row mapping from the
#: adapter's ``.mappings()`` results or an equivalent mapping in tests.
type _MappedIdentityRow = RowMapping | Mapping[str, Any]


@runtime_checkable
class IdentityDiagnosticSink(Protocol):
    """Structural sink the composition root satisfies with its diagnostic logger."""

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None: ...


def bootstrap_lock_key(command: BootstrapIdentityCommand) -> int:
    """Derive the transaction lock key for one bootstrap identity material.

    The material is the exact validated ``username`` and ``workspace_key``
    joined by a separator that cannot appear in either value, so two
    different commands can never produce the same lock material.
    """
    material = f"{command.username}{_LOCK_KEY_SEPARATOR}{command.workspace_key}".encode()
    return signed_first_sha256_word(material)


def bootstrap_lock_statement(command: BootstrapIdentityCommand) -> TextClause:
    """Build the transaction-scoped bootstrap advisory lock statement."""
    return advisory_xact_lock_statement(
        IDENTITY_BOOTSTRAP_LOCK_NAMESPACE,
        bootstrap_lock_key(command),
    )


def hydrate_identity_state(
    user_rows: Sequence[_MappedIdentityRow],
    workspace_rows: Sequence[_MappedIdentityRow],
    device_rows: Sequence[_MappedIdentityRow],
) -> ExistingIdentityState:
    """Convert mapped identity row shapes into the provider-neutral state.

    When exactly one workspace exists, devices are filtered to that
    workspace's ID — later phases may register valid devices elsewhere
    without invalidating replay. Under workspace drift (zero or multiple
    workspaces) every device row passes through unchanged so the classifier
    sees the raw cardinality and fails closed on the conflict.
    """
    devices_for_state = device_rows
    if len(workspace_rows) == 1:
        sole_workspace_id = workspace_rows[0]["workspace_id"]
        devices_for_state = [row for row in device_rows if row["workspace_id"] == sole_workspace_id]
    return ExistingIdentityState(
        users=tuple(
            ExistingIdentityUser(
                user_id=row["user_id"],
                username=row["username"],
                display_name=row["display_name"],
                status=row["status"],
            )
            for row in user_rows
        ),
        workspaces=tuple(
            ExistingIdentityWorkspace(
                workspace_id=row["workspace_id"],
                owner_user_id=row["owner_user_id"],
                workspace_key=row["workspace_key"],
                display_name=row["display_name"],
                status=row["status"],
                created_at=row["created_at"],
            )
            for row in workspace_rows
        ),
        devices=tuple(
            ExistingIdentityDevice(
                device_id=row["device_id"],
                workspace_id=row["workspace_id"],
                user_id=row["user_id"],
                device_name=row["device_name"],
                device_kind=row["device_kind"],
                status=row["status"],
                revoked_at=row["revoked_at"],
            )
            for row in devices_for_state
        ),
    )


def build_identity_audit_values(
    *,
    workspace_id: UUID,
    request_id: UUID,
    occurred_at: datetime,
    client_request_id: UUID | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Build the succeeded ``identity.bootstrap_completed`` audit-row values.

    Actor kind is ``system``, the target is the created workspace, and the
    audit identity is a fresh time-ordered UUIDv7 allocated by the builder.
    """
    return {
        "audit_event_id": uuid7(),
        "workspace_id": workspace_id,
        "actor_kind": IDENTITY_AUDIT_ACTOR_KIND_SYSTEM,
        "actor_id": None,
        "actor_reference": None,
        "action": IDENTITY_BOOTSTRAP_AUDIT_ACTION,
        "target_kind": IDENTITY_AUDIT_TARGET_KIND_WORKSPACE,
        "target_id": workspace_id,
        "request_id": request_id,
        "client_request_id": client_request_id,
        "trace_id": trace_id,
        "result": IDENTITY_AUDIT_RESULT_SUCCEEDED,
        "reason_code": None,
        "safe_diff_hash": None,
        "occurred_at": occurred_at,
    }


def build_identity_rejection_audit_values(
    *,
    workspace_id: UUID,
    request_id: UUID,
    occurred_at: datetime,
    client_request_id: UUID | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Build the standalone ``identity.bootstrap_rejected`` audit-row values.

    The target is the trusted canonical workspace established after the
    conflict rollback; no rejected command value ever enters the row.
    """
    return {
        "audit_event_id": uuid7(),
        "workspace_id": workspace_id,
        "actor_kind": IDENTITY_AUDIT_ACTOR_KIND_SYSTEM,
        "actor_id": None,
        "actor_reference": None,
        "action": IDENTITY_REJECTION_AUDIT_ACTION,
        "target_kind": IDENTITY_AUDIT_TARGET_KIND_WORKSPACE,
        "target_id": workspace_id,
        "request_id": request_id,
        "client_request_id": client_request_id,
        "trace_id": trace_id,
        "result": IDENTITY_AUDIT_RESULT_REJECTED,
        "reason_code": IDENTITY_REJECTION_REASON,
        "safe_diff_hash": None,
        "occurred_at": occurred_at,
    }


class _StateConflictAbort(Exception):
    """Carries a drift conflict out of the open transaction to force rollback.

    The conflict must never let the surrounding ``connection.begin()`` block
    exit normally, because a normal exit commits. Raising this abort rolls
    the (mutation-free) transaction back; the store catches it after the
    block, records the standalone rejection and re-raises the typed error.
    """

    def __init__(self, state: ExistingIdentityState, error: IdentityBootstrapError) -> None:
        super().__init__("identity state conflict aborts the bootstrap transaction")
        self.state = state
        self.error = error


class PostgresqlIdentityBootstrapStore:
    """Atomic PostgreSQL bootstrap transaction (design spec 5.3, 5.4).

    The store takes the composition-owned :class:`AsyncEngine`; it opens no
    connection at construction. Every bootstrap — create or exact replay —
    runs inside one ``READ COMMITTED`` transaction behind the bootstrap
    advisory lock, so concurrent invocations of the same command serialise
    and the second observes the committed state of the first.
    """

    def __init__(
        self, engine: AsyncEngine, *, diagnostics: IdentityDiagnosticSink | None = None
    ) -> None:
        self._engine = engine
        self._diagnostics = diagnostics

    async def bootstrap(
        self, command: BootstrapIdentityCommand, diagnostic_context: DiagnosticContext
    ) -> BootstrapIdentityResult:
        try:
            return await self._bootstrap_once(command, diagnostic_context)
        except _StateConflictAbort as abort:
            # The transaction has already rolled back; record the rejection
            # out of band before the typed error leaves the adapter. A
            # sibling ``except`` clause never catches an exception raised
            # inside this handler, so the rejection recording maps its own
            # database failures here. The mapped database failure replaces
            # the conflict error: the service must never claim an audit that
            # does not exist, and driver text must never leak.
            try:
                await self._record_conflict_rejection(abort.state, command, diagnostic_context)
            except SQLAlchemyError as audit_cause:
                raise map_database_failure(audit_cause, source_id=_NIL_SOURCE_ID) from audit_cause
            raise abort.error from abort
        except SQLAlchemyError as cause:
            raise map_database_failure(cause, source_id=_NIL_SOURCE_ID) from cause

    async def _bootstrap_once(
        self, command: BootstrapIdentityCommand, diagnostic_context: DiagnosticContext
    ) -> BootstrapIdentityResult:
        async with self._engine.connect() as connection:
            try:
                async with connection.begin():
                    await apply_transaction_bounds(connection)
                    await connection.execute(bootstrap_lock_statement(command))
                    state = await self._read_identity_state(connection)
                    if self._is_empty_state(state):
                        return await self._create_identity(connection, command, diagnostic_context)
                    # Replaying performs no mutation and no extra audit row;
                    # the read-only transaction simply commits. A drift
                    # conflict aborts so the transaction rolls back.
                    return classify_existing_identity(state, command)
            except IdentityBootstrapError as error:
                if error.error_code is not ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT:
                    raise
                raise _StateConflictAbort(state, error) from error
        raise AssertionError("unreachable: bootstrap transaction always returns or raises")

    @staticmethod
    def _is_empty_state(state: ExistingIdentityState) -> bool:
        return not state.users and not state.workspaces and not state.devices

    @staticmethod
    async def _read_identity_state(connection: AsyncConnection) -> ExistingIdentityState:
        user_rows = (await connection.execute(sa.select(users))).mappings().all()
        workspace_rows = (await connection.execute(sa.select(workspaces))).mappings().all()
        device_rows = (await connection.execute(sa.select(devices))).mappings().all()
        return hydrate_identity_state(user_rows, workspace_rows, device_rows)

    @staticmethod
    async def _select_now(connection: AsyncConnection) -> datetime:
        """Read the single transaction timestamp shared by all bootstrap rows."""
        result = await connection.execute(sa.text("SELECT now()"))
        occurred_at = result.scalar_one()
        if not isinstance(occurred_at, datetime):  # pragma: no cover - driver contract
            raise TypeError("SELECT now() did not return a datetime")
        return occurred_at

    async def _create_identity(
        self,
        connection: AsyncConnection,
        command: BootstrapIdentityCommand,
        diagnostic_context: DiagnosticContext,
    ) -> BootstrapIdentityResult:
        """Create the canonical identity graph and its audit row (spec 5.3).

        The three UUIDv7 identities are allocated inside the locked
        transaction, every row carries the one shared transaction timestamp
        and a fault after any insert rolls back all four rows.
        """
        committed_at = await self._select_now(connection)
        user_id = uuid7()
        workspace_id = uuid7()
        device_id = uuid7()
        await connection.execute(
            sa.insert(users).values(
                user_id=user_id,
                username=command.username,
                display_name=command.user_display_name,
                status=_USER_STATUS_ACTIVE,
                created_at=committed_at,
                updated_at=committed_at,
            )
        )
        await connection.execute(
            sa.insert(workspaces).values(
                workspace_id=workspace_id,
                owner_user_id=user_id,
                workspace_key=command.workspace_key,
                display_name=command.workspace_display_name,
                status=_WORKSPACE_STATUS_ACTIVE,
                created_at=committed_at,
                updated_at=committed_at,
            )
        )
        await connection.execute(
            sa.insert(devices).values(
                device_id=device_id,
                workspace_id=workspace_id,
                user_id=user_id,
                device_name=command.device_name,
                device_kind=command.device_kind.value,
                status=_DEVICE_STATUS_ACTIVE,
                registered_at=committed_at,
                last_seen_at=None,
                revoked_at=None,
            )
        )
        await connection.execute(
            sa.insert(audit_events).values(
                **build_identity_audit_values(
                    workspace_id=workspace_id,
                    request_id=diagnostic_context.request_id,
                    occurred_at=committed_at,
                    client_request_id=diagnostic_context.client_request_id,
                    trace_id=diagnostic_context.trace.trace_id.value,
                )
            )
        )
        return BootstrapIdentityResult(
            user_id=user_id,
            workspace_id=workspace_id,
            device_id=device_id,
            outcome=BootstrapIdentityOutcome.CREATED,
            committed_at=committed_at,
        )

    async def _record_conflict_rejection(
        self,
        state: ExistingIdentityState,
        command: BootstrapIdentityCommand,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        """Record a rolled-back drift conflict without mutating identity state.

        A standalone rejection audit row is written only when a trusted
        canonical workspace exists; otherwise only the registered
        ``identity_bootstrap_rejected`` diagnostic event is emitted (when a
        sink is wired). No rejected value is ever logged.
        """
        trusted_workspace_id = resolve_trusted_workspace_id(state, command)
        if trusted_workspace_id is None:
            if self._diagnostics is not None:
                self._diagnostics.emit(
                    EventName.IDENTITY_BOOTSTRAP_REJECTED,
                    {"error_code": ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT},
                )
            return
        await self._write_rejection_audit(trusted_workspace_id, diagnostic_context)

    async def _write_rejection_audit(
        self, workspace_id: UUID, diagnostic_context: DiagnosticContext
    ) -> None:
        """Write the standalone rejection audit in its own short transaction."""
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            occurred_at = await self._select_now(connection)
            await connection.execute(
                sa.insert(audit_events).values(
                    **build_identity_rejection_audit_values(
                        workspace_id=workspace_id,
                        request_id=diagnostic_context.request_id,
                        occurred_at=occurred_at,
                        client_request_id=diagnostic_context.client_request_id,
                        trace_id=diagnostic_context.trace.trace_id.value,
                    )
                )
            )
