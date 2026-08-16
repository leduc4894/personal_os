"""Session-anchored authentication transactions.

:class:`WebSessionStore` implements the session-lifecycle half of the
``WebSessionTransactionPort`` over the migrated authentication schema:
``resolve_session`` selects the row behind its unique session-secret hash
``FOR UPDATE``, applies the pure domain authentication decision (state,
credential revision, idle and absolute expiry) and conditionally slides
``last_seen_at`` with the idle expiry clamped to the absolute boundary — one
transaction; ``resolve_challenge_eligible_session`` applies the tolerant
unrevoked-and-unexpired decision the challenge/logout routes of spec 9.2
resolve ``pending_totp`` and ``recovery_limited`` bindings through, without
the active-only gate and without touching activity;
``rotate_session_secrets`` rewrites exactly the closed rotation
cause's timestamps plus both secret hashes behind the prior-hash guard;
``revoke_session`` resolves and revokes behind the presented secret hash in
one transaction, clearing ``authenticated_at`` and ``reauthenticated_at``
together because the schema's state/timestamp matrix rejects a revoked row
that still carries an authenticated moment (binding decision 1).

Every statement is schema-qualified through the Task 6 Core metadata and
parameter-bound; transactions run through the shared bounded-contention
runner from :mod:`postgresql_source_store.authentication_credentials`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.authentication.contracts import WebSessionState
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import (
    ResolvedWebSession,
    RevokedWebSession,
    RevokeWebSessionCommand,
    RotatedWebSessionSecrets,
    RotateWebSessionSecretsCommand,
    SessionRotationCause,
    SessionWindowPolicy,
    StoredWebSession,
    clamp_idle_expiry,
    evaluate_session_authentication,
    is_challenge_eligible_session,
)
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.authentication_credentials import (
    run_authentication_transaction,
)
from postgresql_source_store.tables import user_credentials, web_sessions

#: The state every session write here must clear timestamps for.
_SESSION_STATE_REVOKED: Final[str] = "revoked"

#: The states a rotation may start from, by closed cause.
_ACTIVATION_SOURCE_STATE: Final[WebSessionState] = WebSessionState.PENDING_TOTP
_REAUTHENTICATION_SOURCE_STATE: Final[WebSessionState] = WebSessionState.ACTIVE
_RECOVERY_SOURCE_STATE: Final[WebSessionState] = WebSessionState.RECOVERY_LIMITED

#: Every ``web_sessions`` column the typed row view carries.
_SESSION_ROW_COLUMNS: Final[tuple[Any, ...]] = (
    web_sessions.c.web_session_id,
    web_sessions.c.user_id,
    web_sessions.c.workspace_id,
    web_sessions.c.session_secret_hash,
    web_sessions.c.csrf_secret_hash,
    web_sessions.c.state,
    web_sessions.c.credential_revision,
    web_sessions.c.authentication_method,
    web_sessions.c.created_at,
    web_sessions.c.authenticated_at,
    web_sessions.c.reauthenticated_at,
    web_sessions.c.last_seen_at,
    web_sessions.c.idle_expires_at,
    web_sessions.c.absolute_expires_at,
    web_sessions.c.revoked_at,
    web_sessions.c.revocation_reason,
)


def stored_web_session_from_row(row: Any) -> StoredWebSession:
    """Build the typed session row view from one named result row."""
    return StoredWebSession(
        web_session_id=row.web_session_id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        session_secret_hash=row.session_secret_hash,
        csrf_secret_hash=row.csrf_secret_hash,
        state=WebSessionState(row.state),
        credential_revision=int(row.credential_revision),
        authentication_method=row.authentication_method,
        created_at=row.created_at,
        authenticated_at=row.authenticated_at,
        reauthenticated_at=row.reauthenticated_at,
        last_seen_at=row.last_seen_at,
        idle_expires_at=row.idle_expires_at,
        absolute_expires_at=row.absolute_expires_at,
        revoked_at=row.revoked_at,
        revocation_reason=row.revocation_reason,
    )


class WebSessionStore:
    """Session-lifecycle transactions over the canonical engine.

    The store takes the composition-owned :class:`AsyncEngine`; it opens no
    connection at construction. Each public method is exactly one transaction
    with one commit, and every persisted timestamp is the caller-provided
    single ``database_now`` of its service invocation.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        session_policy: SessionWindowPolicy | None = None,
    ) -> None:
        self._engine = engine
        self._session_policy = (
            session_policy if session_policy is not None else SessionWindowPolicy()
        )

    async def resolve_session(
        self, *, session_secret_hash: str, database_now: datetime
    ) -> ResolvedWebSession:
        """Resolve, validate and conditionally touch one session row."""

        async def operation(connection: AsyncConnection) -> ResolvedWebSession:
            row = await self._select_session_by_secret_hash(connection, session_secret_hash)
            if row is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            session = stored_web_session_from_row(row)
            current_credential_revision = (
                int(row.current_credential_revision)
                if row.current_credential_revision is not None
                else 0
            )
            decision = evaluate_session_authentication(
                session,
                current_credential_revision=current_credential_revision,
                database_now=database_now,
                policy=self._session_policy,
            )
            if not decision.is_authenticated:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            if decision.should_advance_activity and decision.next_idle_expires_at is not None:
                await connection.execute(
                    sa.update(web_sessions)
                    .values(
                        last_seen_at=database_now,
                        idle_expires_at=decision.next_idle_expires_at,
                    )
                    .where(web_sessions.c.web_session_id == session.web_session_id)
                )
                session = replace(
                    session,
                    last_seen_at=database_now,
                    idle_expires_at=decision.next_idle_expires_at,
                )
            return ResolvedWebSession(
                session=session,
                current_credential_revision=current_credential_revision,
                password_hash=row.password_hash,
                database_now=database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    async def resolve_challenge_eligible_session(
        self, *, session_secret_hash: str, database_now: datetime
    ) -> ResolvedWebSession:
        """Resolve one session binding tolerating the pending/recovery states.

        Spec 9.2 lets ``pending_totp`` and ``recovery_limited`` call their own
        challenge routes and logout, so this applies the tolerant decision —
        unrevoked state, both expiry boundaries in the future — without the
        active-only gate and without sliding the idle window.
        """

        async def operation(connection: AsyncConnection) -> ResolvedWebSession:
            row = await self._select_session_by_secret_hash(connection, session_secret_hash)
            if row is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            session = stored_web_session_from_row(row)
            if not is_challenge_eligible_session(session, database_now=database_now):
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            current_credential_revision = (
                int(row.current_credential_revision)
                if row.current_credential_revision is not None
                else 0
            )
            return ResolvedWebSession(
                session=session,
                current_credential_revision=current_credential_revision,
                password_hash=row.password_hash,
                database_now=database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    async def rotate_session_secrets(
        self, command: RotateWebSessionSecretsCommand
    ) -> RotatedWebSessionSecrets:
        """Rotate one session's binding exactly as its closed cause defines."""

        async def operation(connection: AsyncConnection) -> RotatedWebSessionSecrets:
            row = await self._select_session_by_id(connection, command.web_session_id)
            if row is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            session = stored_web_session_from_row(row)
            new_values: dict[str, Any] = {
                "session_secret_hash": command.new_session_secret_hash,
                "csrf_secret_hash": command.new_csrf_secret_hash,
            }
            if command.cause is SessionRotationCause.RECENT_REAUTHENTICATION:
                if (
                    session.state is not _REAUTHENTICATION_SOURCE_STATE
                    or session.authenticated_at is None
                ):
                    raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
                new_values["reauthenticated_at"] = command.database_now
            else:
                expected_source_state = (
                    _ACTIVATION_SOURCE_STATE
                    if command.cause is SessionRotationCause.SESSION_ACTIVATION
                    else _RECOVERY_SOURCE_STATE
                )
                if session.state is not expected_source_state:
                    raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
                new_values["state"] = WebSessionState.ACTIVE.value
                new_values["authentication_method"] = command.target_authentication_method
                new_values["authenticated_at"] = command.database_now
                new_values["reauthenticated_at"] = None
                new_values["idle_expires_at"] = clamp_idle_expiry(
                    command.database_now + self._session_policy.idle_ttl,
                    session.absolute_expires_at,
                )
            rotated = await connection.execute(
                sa.update(web_sessions)
                .values(**new_values)
                .where(
                    web_sessions.c.web_session_id == command.web_session_id,
                    web_sessions.c.session_secret_hash == command.prior_session_secret_hash,
                )
            )
            if rotated.rowcount != 1:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            return RotatedWebSessionSecrets(
                web_session_id=command.web_session_id,
                state=WebSessionState.ACTIVE,
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    async def revoke_session(self, command: RevokeWebSessionCommand) -> RevokedWebSession:
        """Resolve and revoke one session behind its presented secret hash.

        Logout is reachable from ``pending_totp`` and ``recovery_limited``
        too; only an already-revoked or unknown row rejects. The revocation
        clears both authenticated timestamps the state matrix requires.
        """

        async def operation(connection: AsyncConnection) -> RevokedWebSession:
            row = await self._select_session_by_secret_hash(connection, command.session_secret_hash)
            if row is None or row.state == _SESSION_STATE_REVOKED:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            revoked = await connection.execute(
                sa.update(web_sessions)
                .values(
                    state=_SESSION_STATE_REVOKED,
                    revoked_at=command.database_now,
                    revocation_reason=command.revocation_reason,
                    authenticated_at=None,
                    reauthenticated_at=None,
                )
                .where(
                    web_sessions.c.web_session_id == row.web_session_id,
                    web_sessions.c.state != _SESSION_STATE_REVOKED,
                )
            )
            if revoked.rowcount != 1:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            return RevokedWebSession(
                web_session_id=row.web_session_id, revoked_at=command.database_now
            )

        return await run_authentication_transaction(self._engine, operation)

    async def _select_session_by_secret_hash(
        self, connection: AsyncConnection, session_secret_hash: str
    ) -> Any:
        result = await connection.execute(
            self._session_lookup_statement()
            .where(web_sessions.c.session_secret_hash == session_secret_hash)
            .with_for_update(of=web_sessions)
        )
        return result.one_or_none()

    async def _select_session_by_id(self, connection: AsyncConnection, web_session_id: UUID) -> Any:
        result = await connection.execute(
            self._session_lookup_statement()
            .where(web_sessions.c.web_session_id == web_session_id)
            .with_for_update(of=web_sessions)
        )
        return result.one_or_none()

    @staticmethod
    def _session_lookup_statement() -> sa.Select[tuple[Any, ...]]:
        return (
            sa.select(
                *_SESSION_ROW_COLUMNS,
                user_credentials.c.credential_revision.label("current_credential_revision"),
                user_credentials.c.password_hash,
            )
            .select_from(web_sessions)
            .outerjoin(user_credentials, user_credentials.c.user_id == web_sessions.c.user_id)
        )
