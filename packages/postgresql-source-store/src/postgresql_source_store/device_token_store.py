"""Device-token rotation and access-authentication transactions (spec 13).

:class:`DeviceTokenStore` implements the
``DeviceTokenTransactionPort`` over the migrated authentication schema:
``resolve_refresh_predecessor`` is the lock-free pre-read the service uses to
derive the successor material outside the transaction;
``refresh_rotation`` locks the predecessor refresh row and its family,
verifies the presented digest against the row's anchored derivation key,
applies the pure replay classification, then either rotates the predecessor
before inserting the successor pair — advancing the family generation and
the clamped inactivity window without ever extending the absolute expiry —
or returns the committed successor of an exact replay with its anchored
timestamps; confirmed reuse revokes the family, every usable token and the
device, appends exactly one reuse audit row and raises the closed
``device_token_reuse_detected`` rejection.
``authenticate_access_token`` verifies the presented digest under the row's
anchored key, checks the token, family, device, user and workspace state at
the caller's single ``database_now``, and conditionally advances
``devices.last_seen_at`` at most once per five minutes.

Derivation and hashing stay with the caller outside the transactions. Every
statement is schema-qualified through the Task 6 Core metadata and
parameter-bound; transactions run through the shared bounded-contentention
runner from :mod:`postgresql_source_store.authentication_credentials`.
"""

from __future__ import annotations

import hmac as hmac_module
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.authentication.contracts import (
    FIXED_DEVICE_SCOPE,
    AuthenticatedDeviceContext,
    DeviceTokenFamilyState,
    DeviceTokenKind,
    DeviceTokenState,
)
from personal_os.authentication.device_tokens import (
    DEVICE_LAST_SEEN_MAXIMUM_UPDATE_INTERVAL,
    REFRESH_INACTIVITY_LIFETIME,
    AccessTokenAuthenticationCommand,
    AuthenticatedAccessToken,
    CommittedRefreshRotation,
    RefreshPresentationKind,
    RefreshRotationCommand,
    StoredDeviceToken,
    StoredTokenFamily,
    classify_refresh_presentation,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.authentication_credentials import (
    AUDIT_RESULT_SUCCEEDED,
    run_authentication_transaction,
)
from postgresql_source_store.tables import (
    audit_events,
    device_token_families,
    device_tokens,
    devices,
    users,
    workspaces,
)

#: Lifecycle states referenced by the guards.
_TOKEN_STATE_ACTIVE: Final[str] = DeviceTokenState.ACTIVE.value
_TOKEN_STATE_ROTATED: Final[str] = DeviceTokenState.ROTATED.value
_TOKEN_STATE_REVOKED: Final[str] = DeviceTokenState.REVOKED.value
_FAMILY_STATE_ACTIVE: Final[str] = DeviceTokenFamilyState.ACTIVE.value
_FAMILY_STATE_REVOKED: Final[str] = DeviceTokenFamilyState.REVOKED.value
_TOKEN_KIND_REFRESH: Final[str] = DeviceTokenKind.REFRESH.value
_TOKEN_KIND_ACCESS: Final[str] = DeviceTokenKind.ACCESS.value
_USER_STATUS_ACTIVE: Final[str] = "active"
_WORKSPACE_STATUS_ACTIVE: Final[str] = "active"
_DEVICE_STATUS_ACTIVE: Final[str] = "active"
_DEVICE_STATUS_REVOKED: Final[str] = "revoked"

#: Audit actor/target vocabulary and revocation reason of confirmed reuse.
AUDIT_ACTOR_KIND_DEVICE: Final[str] = "device"
AUDIT_TARGET_KIND_DEVICE_TOKEN_FAMILY: Final[str] = "device_token_family"
DEVICE_TOKEN_REUSE_DETECTED_AUDIT_ACTION: Final[str] = "authentication.device_token_reuse_detected"
TOKEN_REUSE_REVOCATION_REASON: Final[str] = "token_reuse"


def stored_device_token_from_row(row: Any) -> StoredDeviceToken:
    """Build the typed token row view from one named result row."""
    return StoredDeviceToken(
        device_token_id=row.device_token_id,
        token_family_id=row.token_family_id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        device_id=row.device_id,
        token_kind=DeviceTokenKind(row.token_kind),
        generation=int(row.generation),
        state=DeviceTokenState(row.state),
        predecessor_token_id=row.predecessor_token_id,
        successor_token_id=row.successor_token_id,
        rotation_id=row.rotation_id,
        derivation_key_id=row.derivation_key_id,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        rotated_at=row.rotated_at,
        revoked_at=row.revoked_at,
    )


def stored_token_family_from_row(row: Any) -> StoredTokenFamily:
    """Build the typed family row view from one named result row."""
    return StoredTokenFamily(
        token_family_id=row.token_family_id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        device_id=row.device_id,
        state=DeviceTokenFamilyState(row.state),
        current_refresh_generation=int(row.current_refresh_generation),
        created_at=row.created_at,
        last_refreshed_at=row.last_refreshed_at,
        inactivity_expires_at=row.inactivity_expires_at,
        absolute_expires_at=row.absolute_expires_at,
        revoked_at=row.revoked_at,
        revocation_reason=row.revocation_reason,
    )


class DeviceTokenStore:
    """Token rotation and access transactions over the canonical engine.

    The store takes the composition-owned :class:`AsyncEngine`; it opens no
    connection at construction. Each public method is exactly one
    transaction with one commit, and every persisted timestamp is the
    caller-provided single ``database_now`` of its service invocation.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # -- predecessor pre-read (spec 13.4) -----------------------------------------------

    async def resolve_refresh_predecessor(self, *, token_id: UUID) -> StoredDeviceToken | None:
        """Read one refresh token row lock-free for outside derivation."""
        statement = sa.select(device_tokens).where(
            device_tokens.c.device_token_id == token_id,
            device_tokens.c.token_kind == _TOKEN_KIND_REFRESH,
        )

        async def operation(connection: AsyncConnection) -> StoredDeviceToken | None:
            row = (await connection.execute(statement)).one_or_none()
            return None if row is None else stored_device_token_from_row(row)

        return await run_authentication_transaction(self._engine, operation)

    # -- refresh rotation (spec 13.4, 13.5) ----------------------------------------------

    async def refresh_rotation(self, command: RefreshRotationCommand) -> CommittedRefreshRotation:
        """Lock, verify and rotate or replay one refresh presentation.

        A confirmed reuse commits its revocation first and surfaces the
        closed ``device_token_reuse_detected`` rejection only after that
        commit: the typed error would otherwise roll the very revocation it
        reports back.
        """

        async def operation(
            connection: AsyncConnection,
        ) -> CommittedRefreshRotation | None:
            locked = await connection.execute(
                sa.select(device_tokens)
                .where(
                    device_tokens.c.device_token_id == command.predecessor_token_id,
                    device_tokens.c.token_kind == _TOKEN_KIND_REFRESH,
                )
                .with_for_update(of=device_tokens)
            )
            predecessor_row = locked.one_or_none()
            if predecessor_row is None:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            presented_hash = command.predecessor_secret_hashes_by_key_id.get(
                predecessor_row.derivation_key_id
            )
            if presented_hash is None or not hmac_module.compare_digest(
                presented_hash, predecessor_row.secret_hash
            ):
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            predecessor = stored_device_token_from_row(predecessor_row)

            locked_family = await connection.execute(
                sa.select(device_token_families)
                .where(device_token_families.c.token_family_id == predecessor.token_family_id)
                .with_for_update(of=device_token_families)
            )
            family_row = locked_family.one_or_none()
            if family_row is None:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            family = stored_token_family_from_row(family_row)

            successor: StoredDeviceToken | None = None
            if predecessor.state is DeviceTokenState.ROTATED:
                successor_row = (
                    await connection.execute(
                        sa.select(device_tokens).where(
                            device_tokens.c.predecessor_token_id == command.predecessor_token_id,
                            device_tokens.c.token_kind == _TOKEN_KIND_REFRESH,
                        )
                    )
                ).one_or_none()
                successor = (
                    None if successor_row is None else stored_device_token_from_row(successor_row)
                )

            presentation = classify_refresh_presentation(
                predecessor=predecessor,
                successor=successor,
                family=family,
                presented_rotation_id=command.rotation_id,
                database_now=command.database_now,
            )
            if presentation is RefreshPresentationKind.REUSE_DETECTED:
                await self._revoke_family_for_reuse(
                    connection,
                    family=family,
                    workspace_id=predecessor.workspace_id,
                    decided_at=command.database_now,
                    diagnostic_context=command.diagnostic_context,
                )
                return None
            if presentation is RefreshPresentationKind.EXACT_REPLAY:
                assert successor is not None
                replay = await self._replay_committed_rotation(
                    connection,
                    predecessor=predecessor,
                    successor=successor,
                    family=family,
                    command=command,
                )
                if replay is None:
                    return None
                return replay
            return await self._commit_new_rotation(
                connection, predecessor=predecessor, family=family, command=command
            )

        committed = await run_authentication_transaction(self._engine, operation)
        if committed is None:
            raise AuthenticationError(ErrorCode.DEVICE_TOKEN_REUSE_DETECTED)
        return committed

    async def _commit_new_rotation(
        self,
        connection: AsyncConnection,
        *,
        predecessor: StoredDeviceToken,
        family: StoredTokenFamily,
        command: RefreshRotationCommand,
    ) -> CommittedRefreshRotation:
        """Rotate the predecessor, then insert and link the successors (13.4).

        The predecessor leaves the active state before any successor exists,
        which the one-current-refresh-generation partial unique demands; the
        successor link lands after the inserts, which the immediate successor
        foreign key demands. The nullable link is exactly why a rotated row
        may exist before — or without — its successor reference.
        """
        successor_generation = predecessor.generation + 1
        absolute_expires_at = family.absolute_expires_at
        assert absolute_expires_at is not None
        refresh_expires_at = min(
            command.database_now + REFRESH_INACTIVITY_LIFETIME, absolute_expires_at
        )
        rotated = await connection.execute(
            sa.update(device_tokens)
            .values(
                state=_TOKEN_STATE_ROTATED,
                rotated_at=command.database_now,
            )
            .where(
                device_tokens.c.device_token_id == predecessor.device_token_id,
                device_tokens.c.state == _TOKEN_STATE_ACTIVE,
            )
        )
        if rotated.rowcount != 1:
            raise AuthenticationError(ErrorCode.DEVICE_TOKEN_REUSE_DETECTED)
        for token_kind, token_id, secret_hash, expires_at in (
            (
                _TOKEN_KIND_REFRESH,
                command.successor_refresh_token_id,
                command.successor_refresh_secret_hash,
                refresh_expires_at,
            ),
            (
                _TOKEN_KIND_ACCESS,
                command.successor_access_token_id,
                command.successor_access_secret_hash,
                command.access_expires_at,
            ),
        ):
            await connection.execute(
                sa.insert(device_tokens).values(
                    device_token_id=token_id,
                    token_family_id=predecessor.token_family_id,
                    user_id=predecessor.user_id,
                    workspace_id=predecessor.workspace_id,
                    device_id=predecessor.device_id,
                    token_kind=token_kind,
                    generation=successor_generation,
                    secret_hash=secret_hash,
                    state=_TOKEN_STATE_ACTIVE,
                    predecessor_token_id=(
                        predecessor.device_token_id if token_kind == _TOKEN_KIND_REFRESH else None
                    ),
                    successor_token_id=None,
                    rotation_id=(
                        command.rotation_id if token_kind == _TOKEN_KIND_REFRESH else None
                    ),
                    derivation_key_id=command.derivation_key_id,
                    issued_at=command.database_now,
                    expires_at=expires_at,
                )
            )
        linked = await connection.execute(
            sa.update(device_tokens)
            .values(successor_token_id=command.successor_refresh_token_id)
            .where(
                device_tokens.c.device_token_id == predecessor.device_token_id,
                device_tokens.c.state == _TOKEN_STATE_ROTATED,
            )
        )
        if linked.rowcount != 1:
            raise AuthenticationError(ErrorCode.DEVICE_TOKEN_REUSE_DETECTED)
        advanced = await connection.execute(
            sa.update(device_token_families)
            .values(
                current_refresh_generation=successor_generation,
                last_refreshed_at=command.database_now,
                inactivity_expires_at=refresh_expires_at,
            )
            .where(
                device_token_families.c.token_family_id == predecessor.token_family_id,
                device_token_families.c.current_refresh_generation == predecessor.generation,
            )
        )
        if advanced.rowcount != 1:
            raise AuthenticationError(ErrorCode.DEVICE_TOKEN_REUSE_DETECTED)
        return CommittedRefreshRotation(
            token_family_id=predecessor.token_family_id,
            successor_refresh_token_id=command.successor_refresh_token_id,
            successor_access_token_id=command.successor_access_token_id,
            successor_generation=successor_generation,
            derivation_key_id=command.derivation_key_id,
            rotated_at=command.database_now,
            access_expires_at=command.access_expires_at,
            refresh_expires_at=refresh_expires_at,
            family_inactivity_expires_at=refresh_expires_at,
            family_absolute_expires_at=absolute_expires_at,
            database_now=command.database_now,
        )

    async def _replay_committed_rotation(
        self,
        connection: AsyncConnection,
        *,
        predecessor: StoredDeviceToken,
        successor: StoredDeviceToken,
        family: StoredTokenFamily,
        command: RefreshRotationCommand,
    ) -> CommittedRefreshRotation | None:
        """Return the committed successor of one exact replay (spec 13.4).

        ``None`` means broken replay lineage: the revocation the caller
        commits stands and the closed reuse rejection follows after it.
        """
        successor_access_row = (
            await connection.execute(
                sa.select(
                    device_tokens.c.device_token_id,
                    device_tokens.c.expires_at,
                )
                .where(
                    device_tokens.c.token_family_id == predecessor.token_family_id,
                    device_tokens.c.token_kind == _TOKEN_KIND_ACCESS,
                    device_tokens.c.generation == successor.generation,
                )
                .order_by(device_tokens.c.issued_at.desc())
                .limit(1)
            )
        ).one_or_none()
        if (
            successor_access_row is None
            or successor.expires_at is None
            or family.inactivity_expires_at is None
            or family.absolute_expires_at is None
            or predecessor.rotated_at is None
        ):
            await self._revoke_family_for_reuse(
                connection,
                family=family,
                workspace_id=predecessor.workspace_id,
                decided_at=command.database_now,
                diagnostic_context=command.diagnostic_context,
            )
            return None
        return CommittedRefreshRotation(
            token_family_id=predecessor.token_family_id,
            successor_refresh_token_id=successor.device_token_id,
            successor_access_token_id=successor_access_row.device_token_id,
            successor_generation=successor.generation,
            derivation_key_id=successor.derivation_key_id,
            rotated_at=predecessor.rotated_at,
            access_expires_at=successor_access_row.expires_at,
            refresh_expires_at=successor.expires_at,
            family_inactivity_expires_at=family.inactivity_expires_at,
            family_absolute_expires_at=family.absolute_expires_at,
            database_now=command.database_now,
        )

    async def _revoke_family_for_reuse(
        self,
        connection: AsyncConnection,
        *,
        family: StoredTokenFamily,
        workspace_id: UUID,
        decided_at: datetime,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        """Revoke the family, its usable tokens and the device (spec 13.5).

        An already-revoked family answers through its terminal state without
        a second revocation or audit row; every write is idempotent behind
        the family row lock this transaction already holds.
        """
        if family.state is DeviceTokenFamilyState.REVOKED:
            return
        await connection.execute(
            sa.update(device_token_families)
            .values(
                state=_FAMILY_STATE_REVOKED,
                revoked_at=decided_at,
                revocation_reason=TOKEN_REUSE_REVOCATION_REASON,
            )
            .where(
                device_token_families.c.token_family_id == family.token_family_id,
                device_token_families.c.state == _FAMILY_STATE_ACTIVE,
            )
        )
        await connection.execute(
            sa.update(device_tokens)
            .values(state=_TOKEN_STATE_REVOKED, revoked_at=decided_at)
            .where(
                device_tokens.c.token_family_id == family.token_family_id,
                device_tokens.c.state == _TOKEN_STATE_ACTIVE,
            )
        )
        await connection.execute(
            sa.update(devices)
            .values(status=_DEVICE_STATUS_REVOKED, revoked_at=decided_at)
            .where(
                devices.c.device_id == family.device_id,
                devices.c.status == _DEVICE_STATUS_ACTIVE,
            )
        )
        await connection.execute(
            sa.insert(audit_events).values(
                audit_event_id=uuid7(),
                workspace_id=workspace_id,
                actor_kind=AUDIT_ACTOR_KIND_DEVICE,
                actor_id=family.device_id,
                actor_reference=None,
                action=DEVICE_TOKEN_REUSE_DETECTED_AUDIT_ACTION,
                target_kind=AUDIT_TARGET_KIND_DEVICE_TOKEN_FAMILY,
                target_id=family.token_family_id,
                request_id=diagnostic_context.request_id,
                client_request_id=diagnostic_context.client_request_id,
                trace_id=diagnostic_context.trace.trace_id.value,
                result=AUDIT_RESULT_SUCCEEDED,
                reason_code=None,
                safe_diff_hash=None,
                occurred_at=decided_at,
            )
        )

    # -- access authentication (spec 13.1) -----------------------------------------------

    async def authenticate_access_token(
        self, command: AccessTokenAuthenticationCommand
    ) -> AuthenticatedAccessToken:
        """Verify one access credential against the current state (13.1)."""

        async def operation(connection: AsyncConnection) -> AuthenticatedAccessToken:
            token_row = (
                await connection.execute(
                    sa.select(device_tokens).where(
                        device_tokens.c.device_token_id == command.token_id,
                        device_tokens.c.token_kind == _TOKEN_KIND_ACCESS,
                    )
                )
            ).one_or_none()
            if token_row is None:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            presented_hash = command.secret_hashes_by_key_id.get(token_row.derivation_key_id)
            if presented_hash is None or not hmac_module.compare_digest(
                presented_hash, token_row.secret_hash
            ):
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            if token_row.state == _TOKEN_STATE_REVOKED:
                raise AuthenticationError(ErrorCode.DEVICE_REVOKED)
            if token_row.state != _TOKEN_STATE_ACTIVE:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            if command.database_now >= token_row.expires_at:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)

            family_row = (
                await connection.execute(
                    sa.select(device_token_families).where(
                        device_token_families.c.token_family_id == token_row.token_family_id
                    )
                )
            ).one_or_none()
            if family_row is None:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            if family_row.state == _FAMILY_STATE_REVOKED:
                raise AuthenticationError(ErrorCode.DEVICE_REVOKED)
            if command.database_now >= family_row.absolute_expires_at:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)

            device_row = (
                await connection.execute(
                    sa.select(devices.c.status, devices.c.last_seen_at).where(
                        devices.c.device_id == token_row.device_id
                    )
                )
            ).one_or_none()
            if device_row is None or device_row.status == _DEVICE_STATUS_REVOKED:
                raise AuthenticationError(ErrorCode.DEVICE_REVOKED)

            user_row = (
                await connection.execute(
                    sa.select(users.c.status).where(users.c.user_id == token_row.user_id)
                )
            ).one_or_none()
            if user_row is None or user_row.status != _USER_STATUS_ACTIVE:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            workspace_row = (
                await connection.execute(
                    sa.select(workspaces.c.status).where(
                        workspaces.c.workspace_id == token_row.workspace_id
                    )
                )
            ).one_or_none()
            if workspace_row is None or workspace_row.status != _WORKSPACE_STATUS_ACTIVE:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)

            last_seen_cutoff = command.database_now - DEVICE_LAST_SEEN_MAXIMUM_UPDATE_INTERVAL
            is_last_seen_stale = (
                device_row.last_seen_at is None or device_row.last_seen_at <= last_seen_cutoff
            )
            if is_last_seen_stale:
                await connection.execute(
                    sa.update(devices)
                    .values(last_seen_at=command.database_now)
                    .where(devices.c.device_id == token_row.device_id)
                )
            return AuthenticatedAccessToken(
                context=AuthenticatedDeviceContext(
                    user_id=token_row.user_id,
                    workspace_id=token_row.workspace_id,
                    device_id=token_row.device_id,
                    scope=FIXED_DEVICE_SCOPE,
                ),
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)
