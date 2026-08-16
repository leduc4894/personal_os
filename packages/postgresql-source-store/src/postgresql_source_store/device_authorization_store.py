"""Device-authorization grant transactions (spec 11.1-11.3).

:class:`DeviceAuthorizationStore` implements the
``DeviceAuthorizationTransactionPort`` over the migrated authentication
schema: ``insert_pending_grant`` writes one pending grant row with only the
caller's HMAC digests and — behind the same commit — counts the creation
attempt into the source's ``grant_creation`` throttle bucket; creation writes
no audit row because the unauthenticated plugin request has no trusted
workspace (spec 21); ``lookup_grant_by_user_code`` resolves one row by its
user-code digest lock-free and resets the ``user_code_lookup`` bucket only
when the pure domain decision says the grant resolves; ``approve_grant`` and
``deny_grant`` lock the grant row ``FOR UPDATE``, apply the pure terminal
decision against the caller's single ``database_now``, update behind the
pending-state guard with a rowcount check so a racing transition commits
exactly one terminal winner, and append exactly one audit event naming the
authenticated user, the deciding session and the grant.

Secret generation and hashing stay with the caller outside the transactions.
Every statement is schema-qualified through the Task 6 Core metadata and
parameter-bound; transactions run through the shared bounded-contention
runner from :mod:`postgresql_source_store.authentication_credentials`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.authentication.contracts import (
    DeviceAuthorizationGrantState,
    DeviceScope,
)
from personal_os.authentication.device_authorization import (
    ApprovedGrant,
    ApproveGrantCommand,
    DeniedGrant,
    DenyGrantCommand,
    DevicePlatformClass,
    InsertedPendingGrant,
    InsertPendingGrantCommand,
    LiveGrantWindow,
    StoredDeviceAuthorizationGrant,
    resolve_lookup_rejection_code,
    resolve_terminal_rejection_code,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import (
    ThrottleBucketKind,
    ThrottleBucketState,
    ThrottleFailureTransition,
    ThrottleWindowPolicy,
    next_login_failure_transition,
)
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.authentication_credentials import (
    AUDIT_ACTOR_KIND_USER,
    AUDIT_RESULT_SUCCEEDED,
    run_authentication_transaction,
)
from postgresql_source_store.tables import (
    audit_events,
    authentication_throttle_buckets,
    device_authorization_grants,
)

#: Lifecycle states referenced by the guards.
_GRANT_STATE_PENDING: Final[str] = DeviceAuthorizationGrantState.PENDING.value

#: Audit target kind and actions of the terminal grant transitions (spec 21).
AUDIT_TARGET_KIND_DEVICE_AUTHORIZATION_GRANT: Final[str] = "device_authorization_grant"
DEVICE_AUTHORIZATION_APPROVED_AUDIT_ACTION: Final[str] = (
    "authentication.device_authorization_approved"
)
DEVICE_AUTHORIZATION_DENIED_AUDIT_ACTION: Final[str] = "authentication.device_authorization_denied"

#: Every ``device_authorization_grants`` column the typed row view carries
#: except the two secret digests, which the domain never consumes.
_GRANT_ROW_COLUMNS: Final[tuple[Any, ...]] = (
    device_authorization_grants.c.grant_id,
    device_authorization_grants.c.client_instance_id,
    device_authorization_grants.c.claimed_device_id,
    device_authorization_grants.c.device_name,
    device_authorization_grants.c.platform_class,
    device_authorization_grants.c.platform_name,
    device_authorization_grants.c.plugin_version,
    device_authorization_grants.c.requested_scope,
    device_authorization_grants.c.state,
    device_authorization_grants.c.created_at,
    device_authorization_grants.c.expires_at,
    device_authorization_grants.c.approved_at,
    device_authorization_grants.c.denied_at,
    device_authorization_grants.c.exchanged_at,
    device_authorization_grants.c.approved_by_user_id,
    device_authorization_grants.c.approved_web_session_id,
)


def stored_device_authorization_grant_from_row(
    row: Any,
) -> StoredDeviceAuthorizationGrant:
    """Build the typed grant row view from one named result row."""
    return StoredDeviceAuthorizationGrant(
        grant_id=row.grant_id,
        client_instance_id=row.client_instance_id,
        claimed_device_id=row.claimed_device_id,
        device_name=row.device_name,
        platform_class=DevicePlatformClass(row.platform_class),
        platform_name=row.platform_name,
        plugin_version=row.plugin_version,
        requested_scope=DeviceScope(row.requested_scope),
        state=DeviceAuthorizationGrantState(row.state),
        created_at=row.created_at,
        expires_at=row.expires_at,
        approved_at=row.approved_at,
        denied_at=row.denied_at,
        exchanged_at=row.exchanged_at,
        approved_by_user_id=row.approved_by_user_id,
        approved_web_session_id=row.approved_web_session_id,
    )


@dataclass(frozen=True, slots=True)
class _CommittedTerminalTransition:
    """The committed outcome of one locked terminal transition."""

    grant_id: UUID
    state: DeviceAuthorizationGrantState
    decided_at: datetime
    database_now: datetime


class DeviceAuthorizationStore:
    """Grant transactions over the canonical engine.

    The store takes the composition-owned :class:`AsyncEngine`; it opens no
    connection at construction. Each public method is exactly one transaction
    with one commit, and every persisted timestamp is the caller-provided
    single ``database_now`` of its service invocation.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        throttle_policy: ThrottleWindowPolicy | None = None,
    ) -> None:
        self._engine = engine
        self._throttle_policy = (
            throttle_policy if throttle_policy is not None else ThrottleWindowPolicy()
        )

    # -- throttle buckets -----------------------------------------------------------

    async def resolve_throttle_bucket(
        self, *, bucket_kind: ThrottleBucketKind, bucket_hash: str
    ) -> ThrottleBucketState | None:
        """Read one grant throttle bucket's counting state lock-free."""

        async def operation(connection: AsyncConnection) -> ThrottleBucketState | None:
            return await self._select_bucket(connection, bucket_kind, bucket_hash)

        return await run_authentication_transaction(self._engine, operation)

    async def record_throttle_attempt(
        self, *, bucket_kind: ThrottleBucketKind, bucket_hash: str, database_now: datetime
    ) -> ThrottleFailureTransition:
        """Lock one bucket row and apply the pure attempt transition."""

        async def operation(connection: AsyncConnection) -> ThrottleFailureTransition:
            return await self._record_bucket_attempt(
                connection, bucket_kind, bucket_hash, database_now
            )

        return await run_authentication_transaction(self._engine, operation)

    # -- creation ---------------------------------------------------------------------

    async def live_grant_window(
        self, *, client_instance_id: UUID, database_now: datetime
    ) -> LiveGrantWindow:
        """Count one client instance's live pending grants (spec 11.1 cap)."""
        statement = sa.select(
            sa.func.count().label("live_grant_count"),
            sa.func.min(device_authorization_grants.c.expires_at).label("earliest_expires_at"),
        ).where(
            device_authorization_grants.c.client_instance_id == client_instance_id,
            device_authorization_grants.c.state == _GRANT_STATE_PENDING,
            device_authorization_grants.c.expires_at > database_now,
        )

        async def operation(connection: AsyncConnection) -> LiveGrantWindow:
            row = (await connection.execute(statement)).one_or_none()
            if row is None:
                return LiveGrantWindow(live_grant_count=0, earliest_expires_at=None)
            return LiveGrantWindow(
                live_grant_count=int(row.live_grant_count),
                earliest_expires_at=row.earliest_expires_at,
            )

        return await run_authentication_transaction(self._engine, operation)

    async def insert_pending_grant(
        self, command: InsertPendingGrantCommand
    ) -> InsertedPendingGrant:
        """Insert one pending grant and count the creation attempt in commit."""

        async def operation(connection: AsyncConnection) -> InsertedPendingGrant:
            if command.creation_bucket_hash is not None:
                await self._record_bucket_attempt(
                    connection,
                    ThrottleBucketKind.GRANT_CREATION,
                    command.creation_bucket_hash,
                    command.database_now,
                )
            await connection.execute(
                sa.insert(device_authorization_grants).values(
                    grant_id=command.grant_id,
                    user_code_hash=command.user_code_hash,
                    polling_secret_hash=command.polling_secret_hash,
                    client_instance_id=command.client_instance_id,
                    claimed_device_id=command.claimed_device_id,
                    device_name=command.device_name,
                    platform_class=command.platform_class,
                    platform_name=command.platform_name,
                    plugin_version=command.plugin_version,
                    requested_scope=command.requested_scope,
                    state=_GRANT_STATE_PENDING,
                    created_at=command.database_now,
                    expires_at=command.expires_at,
                )
            )
            return InsertedPendingGrant(
                grant_id=command.grant_id,
                expires_at=command.expires_at,
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    # -- lookup ------------------------------------------------------------------------

    async def lookup_grant_by_user_code(
        self,
        *,
        user_code_hash: str,
        database_now: datetime,
        reset_bucket_hash: str | None = None,
    ) -> StoredDeviceAuthorizationGrant | None:
        """Resolve one grant by its user-code digest and reset the streak.

        The lookup itself is a read; the only conditional write is the
        ``user_code_lookup`` bucket reset, applied inside the same single
        transaction exactly when the resolved grant passes the pure domain
        lookup decision at ``database_now``.
        """

        async def operation(connection: AsyncConnection) -> StoredDeviceAuthorizationGrant | None:
            row = await self._select_grant_by_user_code_hash(connection, user_code_hash)
            if row is None:
                return None
            grant = stored_device_authorization_grant_from_row(row)
            if (
                reset_bucket_hash is not None
                and resolve_lookup_rejection_code(grant, database_now=database_now) is None
            ):
                await self._reset_bucket(
                    connection,
                    ThrottleBucketKind.USER_CODE_LOOKUP,
                    reset_bucket_hash,
                    database_now,
                )
            return grant

        return await run_authentication_transaction(self._engine, operation)

    # -- terminal transitions ------------------------------------------------------------

    async def approve_grant(self, command: ApproveGrantCommand) -> ApprovedGrant:
        """Lock, recheck and approve exactly one pending grant (spec 11.3)."""
        committed = await self._terminal_transition(
            command,
            target_state=DeviceAuthorizationGrantState.APPROVED,
            audit_action=DEVICE_AUTHORIZATION_APPROVED_AUDIT_ACTION,
        )
        return ApprovedGrant(
            grant_id=committed.grant_id,
            state=committed.state,
            approved_at=committed.decided_at,
            database_now=committed.database_now,
        )

    async def deny_grant(self, command: DenyGrantCommand) -> DeniedGrant:
        """Lock, recheck and deny exactly one pending grant (spec 11.3)."""
        committed = await self._terminal_transition(
            command,
            target_state=DeviceAuthorizationGrantState.DENIED,
            audit_action=DEVICE_AUTHORIZATION_DENIED_AUDIT_ACTION,
        )
        return DeniedGrant(
            grant_id=committed.grant_id,
            state=committed.state,
            denied_at=committed.decided_at,
            database_now=committed.database_now,
        )

    async def _terminal_transition(
        self,
        command: ApproveGrantCommand | DenyGrantCommand,
        *,
        target_state: DeviceAuthorizationGrantState,
        audit_action: str,
    ) -> _CommittedTerminalTransition:
        """Run one locked terminal transition with its single audit row."""
        decided_at = command.database_now
        new_values: dict[str, Any] = {"state": target_state.value}
        if target_state is DeviceAuthorizationGrantState.APPROVED:
            new_values["approved_at"] = decided_at
            new_values["approved_by_user_id"] = command.user_id
            new_values["approved_web_session_id"] = command.web_session_id
        else:
            new_values["denied_at"] = decided_at

        async def operation(
            connection: AsyncConnection,
        ) -> _CommittedTerminalTransition:
            locked = await connection.execute(
                sa.select(*_GRANT_ROW_COLUMNS)
                .where(device_authorization_grants.c.grant_id == command.grant_id)
                .with_for_update(of=device_authorization_grants)
            )
            row = locked.one_or_none()
            rejection_code = resolve_terminal_rejection_code(
                None if row is None else stored_device_authorization_grant_from_row(row),
                database_now=command.database_now,
            )
            if rejection_code is not None:
                raise AuthenticationError(rejection_code)
            updated = await connection.execute(
                sa.update(device_authorization_grants)
                .values(**new_values)
                .where(
                    device_authorization_grants.c.grant_id == command.grant_id,
                    device_authorization_grants.c.state == _GRANT_STATE_PENDING,
                )
            )
            if updated.rowcount != 1:
                # A racing transition committed first under the row lock
                # recheck; exactly one terminal winner exists.
                raise AuthenticationError(ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID)
            await _append_grant_audit_event(
                connection,
                diagnostic_context=command.diagnostic_context,
                workspace_id=command.workspace_id,
                user_id=command.user_id,
                grant_id=command.grant_id,
                action=audit_action,
                occurred_at=decided_at,
            )
            return _CommittedTerminalTransition(
                grant_id=command.grant_id,
                state=target_state,
                decided_at=decided_at,
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    # -- internal helpers ----------------------------------------------------------------

    async def _select_grant_by_user_code_hash(
        self, connection: AsyncConnection, user_code_hash: str
    ) -> Any:
        result = await connection.execute(
            sa.select(*_GRANT_ROW_COLUMNS).where(
                device_authorization_grants.c.user_code_hash == user_code_hash
            )
        )
        return result.one_or_none()

    async def _select_bucket(
        self,
        connection: AsyncConnection,
        bucket_kind: ThrottleBucketKind,
        bucket_hash: str,
    ) -> ThrottleBucketState | None:
        result = await connection.execute(
            sa.select(
                authentication_throttle_buckets.c.window_started_at,
                authentication_throttle_buckets.c.failed_attempt_count,
                authentication_throttle_buckets.c.locked_until,
            ).where(
                authentication_throttle_buckets.c.bucket_kind == bucket_kind.value,
                authentication_throttle_buckets.c.bucket_hash == bucket_hash,
            )
        )
        row = result.one_or_none()
        if row is None:
            return None
        return ThrottleBucketState(
            window_started_at=row.window_started_at,
            failed_attempt_count=int(row.failed_attempt_count),
            locked_until=row.locked_until,
        )

    async def _record_bucket_attempt(
        self,
        connection: AsyncConnection,
        bucket_kind: ThrottleBucketKind,
        bucket_hash: str,
        database_now: datetime,
    ) -> ThrottleFailureTransition:
        """Lock one bucket row and apply the pure attempt transition."""
        locked = await connection.execute(
            sa.select(
                authentication_throttle_buckets.c.throttle_bucket_id,
                authentication_throttle_buckets.c.window_started_at,
                authentication_throttle_buckets.c.failed_attempt_count,
                authentication_throttle_buckets.c.locked_until,
            )
            .where(
                authentication_throttle_buckets.c.bucket_kind == bucket_kind.value,
                authentication_throttle_buckets.c.bucket_hash == bucket_hash,
            )
            .with_for_update()
        )
        row = locked.one_or_none()
        previous = (
            None
            if row is None
            else ThrottleBucketState(
                window_started_at=row.window_started_at,
                failed_attempt_count=int(row.failed_attempt_count),
                locked_until=row.locked_until,
            )
        )
        transition = next_login_failure_transition(
            previous, database_now=database_now, policy=self._throttle_policy
        )
        if row is None:
            await connection.execute(
                sa.insert(authentication_throttle_buckets).values(
                    throttle_bucket_id=uuid7(),
                    bucket_kind=bucket_kind.value,
                    bucket_hash=bucket_hash,
                    window_started_at=transition.window_started_at,
                    failed_attempt_count=transition.failed_attempt_count,
                    locked_until=transition.locked_until,
                    updated_at=database_now,
                )
            )
        else:
            await connection.execute(
                sa.update(authentication_throttle_buckets)
                .values(
                    window_started_at=transition.window_started_at,
                    failed_attempt_count=transition.failed_attempt_count,
                    locked_until=transition.locked_until,
                    updated_at=database_now,
                )
                .where(
                    authentication_throttle_buckets.c.throttle_bucket_id == row.throttle_bucket_id
                )
            )
        return transition

    @staticmethod
    async def _reset_bucket(
        connection: AsyncConnection,
        bucket_kind: ThrottleBucketKind,
        bucket_hash: str,
        database_now: datetime,
    ) -> None:
        await connection.execute(
            sa.update(authentication_throttle_buckets)
            .values(
                window_started_at=database_now,
                failed_attempt_count=0,
                locked_until=None,
                updated_at=database_now,
            )
            .where(
                authentication_throttle_buckets.c.bucket_kind == bucket_kind.value,
                authentication_throttle_buckets.c.bucket_hash == bucket_hash,
            )
        )


async def _append_grant_audit_event(
    connection: AsyncConnection,
    *,
    diagnostic_context: DiagnosticContext,
    workspace_id: UUID,
    user_id: UUID,
    grant_id: UUID,
    action: str,
    occurred_at: datetime,
) -> None:
    """Insert one append-only terminal-transition audit row (spec 21)."""
    await connection.execute(
        sa.insert(audit_events).values(
            audit_event_id=uuid7(),
            workspace_id=workspace_id,
            actor_kind=AUDIT_ACTOR_KIND_USER,
            actor_id=user_id,
            actor_reference=None,
            action=action,
            target_kind=AUDIT_TARGET_KIND_DEVICE_AUTHORIZATION_GRANT,
            target_id=grant_id,
            request_id=diagnostic_context.request_id,
            client_request_id=diagnostic_context.client_request_id,
            trace_id=diagnostic_context.trace.trace_id.value,
            result=AUDIT_RESULT_SUCCEEDED,
            reason_code=None,
            safe_diff_hash=None,
            occurred_at=occurred_at,
        )
    )
