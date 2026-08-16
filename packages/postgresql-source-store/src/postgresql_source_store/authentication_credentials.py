"""Credential- and throttle-anchored authentication transactions.

:class:`CredentialStore` implements the credential-anchored half of the
``CredentialTransactionPort`` over the migrated authentication schema:
``resolve_login_material`` reads one username's credential, trust and throttle
state lock-free; ``record_login_failure`` locks the credential row of a known
user, applies the pure domain throttle transition under both bucket row locks
and appends the rejected-attempt audit only behind the trusted account
boundary; ``commit_login_success`` rechecks the active user/workspace and the
credential revision under the credential row lock, decides ``active`` versus
``pending_totp`` from the live TOTP state, resets the credential streak,
optionally upgrades an obsolete Argon2id hash, inserts the session row and
appends the succeeded audit — one commit; ``change_password`` bumps the
revision, revokes every other session with cleared authenticated timestamps
and rotates the current binding without touching devices; and
``required_key_ids`` unions the key IDs referenced by active TOTP ciphertext
with the replay-eligible grant/token state so startup can fail before bind
when the configured keyring omits one (spec 20.1).

The protected operator operations of design sections 7.1 and 7.2 live here
too: ``resolve_web_credential_status`` reads one canonical username's
enrollment state lock-free; ``enroll_web_credential`` locks the canonical
identity row, refuses any existing credential, inserts ``user_credentials``
revision 1 and appends the enrollment audit in one transaction with the
Argon2id hash computed by the caller outside it; and
``reset_web_authentication`` closes every authentication surface of one
canonical identity in exactly one transaction — password and revision
replaced, TOTP credentials replaced with their recovery codes disabled, every
web session revoked with cleared authenticated timestamps, every device,
token family and still-active token revoked, every pending grant plus the
identity's approved-but-unexchanged grants denied, and one append-only reset
audit row — returning the closed counts of each closed surface.

Every statement is schema-qualified through the Task 6 Core metadata and
parameter-bound; driver failures are classified through
:mod:`postgresql_source_store.error_mapping`, retried only for bounded
contention, and never leave the adapter as driver text.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.authentication.contracts import WebSessionState
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import (
    REVOCATION_REASON_PASSWORD_CHANGED,
    ChangedPassword,
    ChangePasswordCommand,
    CommitLoginSuccessCommand,
    CommittedLoginSuccess,
    RecordedLoginFailure,
    RecordLoginFailureCommand,
    ResolvedLoginMaterial,
    ThrottleBucketKind,
    ThrottleBucketState,
    ThrottleWindowPolicy,
    next_login_failure_transition,
)
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import (
    RETRY_JITTER_MAXIMUM_SECONDS,
    RETRY_JITTER_MINIMUM_SECONDS,
    DatabaseFailureKind,
    classify_database_failure,
)
from postgresql_source_store.tables import (
    audit_events,
    authentication_throttle_buckets,
    device_authorization_grants,
    device_token_families,
    device_tokens,
    devices,
    totp_credentials,
    totp_recovery_codes,
    user_credentials,
    users,
    web_sessions,
    workspaces,
)

#: Audit action tokens of the three credential-anchored transitions.
LOGIN_SUCCEEDED_AUDIT_ACTION: Final[str] = "authentication.login_succeeded"
LOGIN_REJECTED_AUDIT_ACTION: Final[str] = "authentication.login_rejected"
PASSWORD_CHANGED_AUDIT_ACTION: Final[str] = "authentication.password_changed"
WEB_CREDENTIAL_ENROLLED_AUDIT_ACTION: Final[str] = "authentication.web_credential_enrolled"
WEB_AUTHENTICATION_RESET_AUDIT_ACTION: Final[str] = "authentication.web_authentication_reset"

#: Audit target and actor vocabulary of the authentication transitions.
AUDIT_TARGET_KIND_USER_CREDENTIAL: Final[str] = "user_credential"
AUDIT_ACTOR_KIND_USER: Final[str] = "user"
AUDIT_RESULT_SUCCEEDED: Final[str] = "succeeded"
AUDIT_RESULT_REJECTED: Final[str] = "rejected"
REASON_INVALID_CREDENTIALS: Final[str] = "invalid_credentials"

#: Lifecycle states referenced by the rechecks and lookups.
_USER_STATUS_ACTIVE: Final[str] = "active"
_WORKSPACE_STATUS_ACTIVE: Final[str] = "active"
_TOTP_STATE_ACTIVE: Final[str] = "active"
_TOTP_STATE_PENDING: Final[str] = "pending"
_TOTP_STATE_REPLACED: Final[str] = "replaced"
_SESSION_STATE_REVOKED: Final[str] = "revoked"
_DEVICE_STATUS_ACTIVE: Final[str] = "active"
_DEVICE_STATUS_REVOKED: Final[str] = "revoked"
_TOKEN_FAMILY_STATE_ACTIVE: Final[str] = "active"
_TOKEN_FAMILY_STATE_REVOKED: Final[str] = "revoked"
_REFRESH_TOKEN_KIND: Final[str] = "refresh"
_TOKEN_STATE_ACTIVE: Final[str] = "active"
_TOKEN_STATE_REVOKED: Final[str] = "revoked"
_GRANT_STATE_PENDING: Final[str] = "pending"
_GRANT_STATE_APPROVED: Final[str] = "approved"
_GRANT_STATE_DENIED: Final[str] = "denied"
_GRANT_STATES_WITH_REPLAY_STATE: Final[tuple[str, ...]] = ("approved", "exchanged")

#: Revocation reason of every surface the emergency reset closes (spec 7.2).
EMERGENCY_RESET_REVOCATION_REASON: Final[str] = "emergency_reset"

#: Bounded contention retry bounds, mirroring the canonical policy.
_MAXIMUM_TRANSACTION_ATTEMPTS: Final[int] = 3


async def run_authentication_transaction[TransactionResultT](
    engine: AsyncEngine,
    operation: Callable[[AsyncConnection], Awaitable[TransactionResultT]],
) -> TransactionResultT:
    """Run one ``READ COMMITTED`` transaction with the pinned local bounds.

    Typed application errors (business rejections) propagate untouched after
    rolling the transaction back; deadlock, serialization and lock-contention
    failures retry at most three times with cancellable jitter; every other
    failure leaves the adapter as the safe ``internal_error`` so SQLSTATE, SQL
    and driver text never cross the boundary.
    """
    for attempt in range(1, _MAXIMUM_TRANSACTION_ATTEMPTS + 1):
        try:
            async with (
                engine.connect() as connection,
                connection.begin(),
            ):
                await apply_transaction_bounds(connection)
                return await operation(connection)
        except ApplicationError:
            raise
        except Exception as cause:
            failure_kind = classify_database_failure(cause)
            if (
                failure_kind is DatabaseFailureKind.CONTENTION
                and attempt < _MAXIMUM_TRANSACTION_ATTEMPTS
            ):
                await asyncio.sleep(
                    random.uniform(RETRY_JITTER_MINIMUM_SECONDS, RETRY_JITTER_MAXIMUM_SECONDS)
                )
                continue
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause
    raise AssertionError("authentication transaction attempts exhausted without a result")


# --- protected operator commands (spec 7.1, 7.2) ------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedWebCredentialStatus:
    """One canonical username's enrollment state; the revision is the whole truth.

    ``credential_revision`` is ``None`` exactly when no Web credential exists;
    the non-secret status command prints nothing beyond that flag and number.
    """

    user_id: UUID
    workspace_id: UUID | None
    credential_revision: int | None


@dataclass(frozen=True, slots=True)
class EnrollWebCredentialCommand:
    """One initial enrollment's transactional write (spec 7.1).

    The Argon2id hash is computed by the caller outside the transaction; the
    command resolves, refuses and inserts behind the canonical identity lock.
    """

    username: str
    password_hash: str = field(repr=False)
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class EnrolledWebCredential:
    """The committed identity of one accepted enrollment."""

    user_id: UUID
    workspace_id: UUID
    credential_revision: int
    database_now: datetime


@dataclass(frozen=True, slots=True)
class ResetWebAuthenticationCommand:
    """One emergency reset's transactional writes (spec 7.2).

    The new Argon2id hash is computed by the caller outside the transaction;
    the typed confirmation is validated at the input boundary before this
    command is ever built.
    """

    username: str
    new_password_hash: str = field(repr=False)
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class ResetWebAuthentication:
    """The committed outcome of one emergency reset with closed counts."""

    user_id: UUID
    workspace_id: UUID
    credential_revision: int
    replaced_totp_credential_count: int
    disabled_recovery_code_count: int
    revoked_web_session_count: int
    revoked_device_count: int
    revoked_token_family_count: int
    revoked_device_token_count: int
    denied_grant_count: int
    database_now: datetime


class CredentialStore:
    """Credential/throttle transactions over the canonical engine.

    The store takes the composition-owned :class:`AsyncEngine`; it opens no
    connection at construction. Each public method is exactly one transaction
    with one commit; every persisted timestamp is the caller-provided single
    ``database_now`` of its service invocation.
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

    async def resolve_login_material(
        self, *, username: str, username_bucket_hash: str, source_bucket_hash: str
    ) -> ResolvedLoginMaterial:
        """Read one username's credential, trust and throttle state lock-free."""

        async def operation(connection: AsyncConnection) -> ResolvedLoginMaterial:
            account_result = await connection.execute(
                sa.select(
                    users.c.user_id,
                    users.c.status.label("user_status"),
                    user_credentials.c.workspace_id,
                    user_credentials.c.password_hash,
                    user_credentials.c.credential_revision,
                    workspaces.c.status.label("workspace_status"),
                )
                .select_from(users)
                .outerjoin(user_credentials, user_credentials.c.user_id == users.c.user_id)
                .outerjoin(
                    workspaces,
                    sa.and_(
                        workspaces.c.workspace_id == user_credentials.c.workspace_id,
                        workspaces.c.owner_user_id == users.c.user_id,
                    ),
                )
                .where(users.c.username == username)
            )
            account = account_result.one_or_none()
            username_bucket = await self._select_bucket(
                connection, ThrottleBucketKind.LOGIN_USERNAME, username_bucket_hash
            )
            source_bucket = await self._select_bucket(
                connection, ThrottleBucketKind.LOGIN_SOURCE, source_bucket_hash
            )
            if (
                account is None
                or account.workspace_id is None
                or account.password_hash is None
                or account.credential_revision is None
            ):
                return ResolvedLoginMaterial(
                    user_id=None,
                    workspace_id=None,
                    is_trusted_account=False,
                    password_hash=None,
                    credential_revision=None,
                    username_bucket=username_bucket,
                    source_bucket=source_bucket,
                )
            return ResolvedLoginMaterial(
                user_id=account.user_id,
                workspace_id=account.workspace_id,
                is_trusted_account=(
                    account.user_status == _USER_STATUS_ACTIVE
                    and account.workspace_status == _WORKSPACE_STATUS_ACTIVE
                ),
                password_hash=account.password_hash,
                credential_revision=int(account.credential_revision),
                username_bucket=username_bucket,
                source_bucket=source_bucket,
            )

        return await run_authentication_transaction(self._engine, operation)

    async def record_login_failure(
        self, command: RecordLoginFailureCommand
    ) -> RecordedLoginFailure:
        """Commit one rejected attempt: both buckets plus the trusted audit."""

        async def operation(connection: AsyncConnection) -> RecordedLoginFailure:
            was_audited = False
            if command.user_id is not None and command.workspace_id is not None:
                was_audited = await self._is_account_trusted_locked(
                    connection, command.user_id, command.workspace_id
                )
            username_bucket = await self._record_bucket_failure(
                connection,
                ThrottleBucketKind.LOGIN_USERNAME,
                command.username_bucket_hash,
                command.database_now,
            )
            source_bucket = await self._record_bucket_failure(
                connection,
                ThrottleBucketKind.LOGIN_SOURCE,
                command.source_bucket_hash,
                command.database_now,
            )
            if was_audited:
                audited_user_id = command.user_id
                audited_workspace_id = command.workspace_id
                assert audited_user_id is not None and audited_workspace_id is not None
                await _append_audit_event(
                    connection,
                    diagnostic_context=command.diagnostic_context,
                    workspace_id=audited_workspace_id,
                    user_id=audited_user_id,
                    action=LOGIN_REJECTED_AUDIT_ACTION,
                    result=AUDIT_RESULT_REJECTED,
                    reason_code=REASON_INVALID_CREDENTIALS,
                    occurred_at=command.database_now,
                )
            return RecordedLoginFailure(
                username_bucket=username_bucket,
                source_bucket=source_bucket,
                was_audited=was_audited,
            )

        return await run_authentication_transaction(self._engine, operation)

    async def commit_login_success(
        self, command: CommitLoginSuccessCommand
    ) -> CommittedLoginSuccess:
        """Commit one accepted login: recheck, streak reset, session, audit."""

        async def operation(connection: AsyncConnection) -> CommittedLoginSuccess:
            recheck = await self._select_locked_credential(
                connection, command.user_id, command.workspace_id
            )
            if (
                recheck is None
                or int(recheck.credential_revision) != command.expected_credential_revision
            ):
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            state = (
                WebSessionState.PENDING_TOTP
                if bool(recheck.has_active_totp)
                else WebSessionState.ACTIVE
            )
            authenticated_at = command.database_now if state is WebSessionState.ACTIVE else None
            idle_expires_at = (
                command.pending_totp_idle_expires_at
                if state is WebSessionState.PENDING_TOTP
                else command.active_idle_expires_at
            )
            await connection.execute(
                sa.update(authentication_throttle_buckets)
                .values(
                    window_started_at=command.database_now,
                    failed_attempt_count=0,
                    locked_until=None,
                    updated_at=command.database_now,
                )
                .where(
                    authentication_throttle_buckets.c.bucket_kind
                    == ThrottleBucketKind.LOGIN_USERNAME.value,
                    authentication_throttle_buckets.c.bucket_hash == command.username_bucket_hash,
                )
            )
            if command.upgraded_password_hash is not None:
                await connection.execute(
                    sa.update(user_credentials)
                    .values(
                        password_hash=command.upgraded_password_hash,
                        updated_at=command.database_now,
                    )
                    .where(user_credentials.c.user_id == command.user_id)
                )
            await connection.execute(
                sa.insert(web_sessions).values(
                    web_session_id=command.web_session_id,
                    user_id=command.user_id,
                    workspace_id=command.workspace_id,
                    session_secret_hash=command.session_secret_hash,
                    csrf_secret_hash=command.csrf_secret_hash,
                    state=state.value,
                    credential_revision=command.expected_credential_revision,
                    authentication_method=command.authentication_method,
                    created_at=command.database_now,
                    authenticated_at=authenticated_at,
                    reauthenticated_at=None,
                    last_seen_at=None,
                    idle_expires_at=idle_expires_at,
                    absolute_expires_at=command.absolute_expires_at,
                    revoked_at=None,
                    revocation_reason=None,
                )
            )
            await _append_audit_event(
                connection,
                diagnostic_context=command.diagnostic_context,
                workspace_id=command.workspace_id,
                user_id=command.user_id,
                action=LOGIN_SUCCEEDED_AUDIT_ACTION,
                result=AUDIT_RESULT_SUCCEEDED,
                reason_code=None,
                occurred_at=command.database_now,
            )
            return CommittedLoginSuccess(
                web_session_id=command.web_session_id,
                user_id=command.user_id,
                workspace_id=command.workspace_id,
                state=state,
                credential_revision=command.expected_credential_revision,
                authenticated_at=authenticated_at,
                idle_expires_at=idle_expires_at,
                absolute_expires_at=command.absolute_expires_at,
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    async def change_password(self, command: ChangePasswordCommand) -> ChangedPassword:
        """Commit one password change: revision, revocations, rotation, audit."""

        async def operation(connection: AsyncConnection) -> ChangedPassword:
            recheck = await self._select_locked_credential(
                connection, command.user_id, command.workspace_id
            )
            if recheck is None or int(recheck.credential_revision) != (
                command.expected_credential_revision
            ):
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            next_credential_revision = command.expected_credential_revision + 1
            await connection.execute(
                sa.update(user_credentials)
                .values(
                    password_hash=command.new_password_hash,
                    credential_revision=next_credential_revision,
                    password_changed_at=command.database_now,
                    updated_at=command.database_now,
                )
                .where(user_credentials.c.user_id == command.user_id)
            )
            revoked = await connection.execute(
                sa.update(web_sessions)
                .values(
                    state=_SESSION_STATE_REVOKED,
                    revoked_at=command.database_now,
                    revocation_reason=REVOCATION_REASON_PASSWORD_CHANGED,
                    authenticated_at=None,
                    reauthenticated_at=None,
                )
                .where(
                    web_sessions.c.user_id == command.user_id,
                    web_sessions.c.web_session_id != command.current_web_session_id,
                    web_sessions.c.state != _SESSION_STATE_REVOKED,
                )
            )
            rotated = await connection.execute(
                sa.update(web_sessions)
                .values(
                    session_secret_hash=command.new_session_secret_hash,
                    csrf_secret_hash=command.new_csrf_secret_hash,
                    credential_revision=next_credential_revision,
                )
                .where(
                    web_sessions.c.web_session_id == command.current_web_session_id,
                    web_sessions.c.user_id == command.user_id,
                    web_sessions.c.session_secret_hash == command.prior_session_secret_hash,
                    web_sessions.c.state != _SESSION_STATE_REVOKED,
                )
            )
            if rotated.rowcount != 1:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            await _append_audit_event(
                connection,
                diagnostic_context=command.diagnostic_context,
                workspace_id=command.workspace_id,
                user_id=command.user_id,
                action=PASSWORD_CHANGED_AUDIT_ACTION,
                result=AUDIT_RESULT_SUCCEEDED,
                reason_code=None,
                occurred_at=command.database_now,
            )
            return ChangedPassword(
                current_web_session_id=command.current_web_session_id,
                credential_revision=next_credential_revision,
                revoked_session_count=int(revoked.rowcount),
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    async def required_key_ids(self, database_now: datetime) -> frozenset[str]:
        """Every key ID referenced by decryptable or replay-eligible state.

        Active and pending TOTP ciphertext keeps referencing its ``key_id``
        until re-encrypted; replaced credentials never decrypt again. Refresh
        tokens still active and unexpired carry replay-detectable rotation
        state under their derivation key, and grants that have carried a
        derivation key stay referenced while unexpired and not denied. The
        composition root fails ``serve`` before bind when the configured
        keyring omits any returned ID (spec 20.1).
        """
        totp_statement = sa.select(totp_credentials.c.key_id.label("referenced_key_id")).where(
            totp_credentials.c.state.in_((_TOTP_STATE_ACTIVE, _TOTP_STATE_PENDING))
        )
        token_statement = sa.select(
            device_tokens.c.derivation_key_id.label("referenced_key_id")
        ).where(
            device_tokens.c.token_kind == _REFRESH_TOKEN_KIND,
            device_tokens.c.state == _TOKEN_STATE_ACTIVE,
            device_tokens.c.expires_at > database_now,
        )
        grant_statement = sa.select(
            device_authorization_grants.c.derivation_key_id.label("referenced_key_id")
        ).where(
            device_authorization_grants.c.derivation_key_id.is_not(None),
            device_authorization_grants.c.state.in_(_GRANT_STATES_WITH_REPLAY_STATE),
            device_authorization_grants.c.expires_at > database_now,
        )

        async def operation(connection: AsyncConnection) -> frozenset[str]:
            result = await connection.execute(
                totp_statement.union(token_statement, grant_statement)
            )
            return frozenset(str(row.referenced_key_id) for row in result.all())

        return await run_authentication_transaction(self._engine, operation)

    async def resolve_web_credential_status(self, *, username: str) -> ResolvedWebCredentialStatus:
        """Read one canonical username's enrollment state lock-free (spec 7.1).

        An unresolvable username fails closed as the generic
        ``authentication_failed`` rejection, mirroring the login contract; a
        resolved username without a credential row reports ``None`` so the
        status surface never distinguishes more than enrolled versus not.
        """

        async def operation(connection: AsyncConnection) -> ResolvedWebCredentialStatus:
            result = await connection.execute(
                sa.select(
                    users.c.user_id,
                    user_credentials.c.workspace_id,
                    user_credentials.c.credential_revision,
                )
                .select_from(users)
                .outerjoin(user_credentials, user_credentials.c.user_id == users.c.user_id)
                .where(users.c.username == username)
            )
            row = result.one_or_none()
            if row is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            return ResolvedWebCredentialStatus(
                user_id=row.user_id,
                workspace_id=row.workspace_id,
                credential_revision=(
                    int(row.credential_revision) if row.credential_revision is not None else None
                ),
            )

        return await run_authentication_transaction(self._engine, operation)

    async def enroll_web_credential(
        self, command: EnrollWebCredentialCommand
    ) -> EnrolledWebCredential:
        """Insert the initial Web credential revision 1 (spec 7.1).

        One transaction locks the canonical identity row, resolves exactly one
        active user/workspace, refuses when any credential already exists,
        inserts revision 1 with the caller's pre-computed Argon2id hash and
        appends the enrollment audit. Enrollment is intentionally not
        create-or-return: a concurrent or repeated attempt serialises on the
        identity lock and leaves the existing credential untouched.
        """

        async def operation(connection: AsyncConnection) -> EnrolledWebCredential:
            identity = await _select_locked_canonical_identity(connection, command.username)
            if identity is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            existing = await connection.execute(
                sa.select(user_credentials.c.user_id).where(
                    user_credentials.c.user_id == identity.user_id
                )
            )
            if existing.one_or_none() is not None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            await connection.execute(
                sa.insert(user_credentials).values(
                    user_id=identity.user_id,
                    workspace_id=identity.workspace_id,
                    password_hash=command.password_hash,
                    credential_revision=1,
                    password_changed_at=command.database_now,
                    created_at=command.database_now,
                    updated_at=command.database_now,
                )
            )
            await _append_audit_event(
                connection,
                diagnostic_context=command.diagnostic_context,
                workspace_id=identity.workspace_id,
                user_id=identity.user_id,
                action=WEB_CREDENTIAL_ENROLLED_AUDIT_ACTION,
                result=AUDIT_RESULT_SUCCEEDED,
                reason_code=None,
                occurred_at=command.database_now,
            )
            return EnrolledWebCredential(
                user_id=identity.user_id,
                workspace_id=identity.workspace_id,
                credential_revision=1,
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    async def reset_web_authentication(
        self, command: ResetWebAuthenticationCommand
    ) -> ResetWebAuthentication:
        """Close every authentication surface of one identity (spec 7.2).

        Exactly one transaction: the canonical identity row locks first, the
        credential row locks second, then the password hash and revision are
        replaced, every active or pending TOTP credential becomes ``replaced``
        with its unused recovery codes disabled, every web session is revoked
        with both authenticated timestamps cleared (the schema state matrix
        demands it), every device, token family and still-active token is
        revoked, every pending grant plus this identity's
        approved-but-unexchanged grants is denied, and one append-only reset
        audit row lands with the commit. Pending grants carry no user linkage
        until approval, so they are denied deployment-wide — the
        single-operator reading of the personal OS. Vault data, device audit
        rows and sync queue data are never deleted or touched.
        """

        async def operation(connection: AsyncConnection) -> ResetWebAuthentication:
            identity = await _select_locked_canonical_identity(connection, command.username)
            if identity is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            locked_credential = await connection.execute(
                sa.select(user_credentials.c.workspace_id, user_credentials.c.credential_revision)
                .where(user_credentials.c.user_id == identity.user_id)
                .with_for_update(of=user_credentials)
            )
            credential = locked_credential.one_or_none()
            if credential is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            next_credential_revision = int(credential.credential_revision) + 1
            await connection.execute(
                sa.update(user_credentials)
                .values(
                    password_hash=command.new_password_hash,
                    credential_revision=next_credential_revision,
                    totp_prompt_dismissed_at=None,
                    password_changed_at=command.database_now,
                    updated_at=command.database_now,
                )
                .where(user_credentials.c.user_id == identity.user_id)
            )
            replaced_totp = await connection.execute(
                sa.update(totp_credentials)
                .values(
                    state=_TOTP_STATE_REPLACED,
                    replaced_at=command.database_now,
                    enrollment_expires_at=None,
                )
                .where(
                    totp_credentials.c.user_id == identity.user_id,
                    totp_credentials.c.state.in_((_TOTP_STATE_ACTIVE, _TOTP_STATE_PENDING)),
                )
            )
            disabled_recovery = await connection.execute(
                sa.update(totp_recovery_codes)
                .values(used_at=command.database_now)
                .where(
                    totp_recovery_codes.c.user_id == identity.user_id,
                    totp_recovery_codes.c.used_at.is_(None),
                )
            )
            revoked_sessions = await connection.execute(
                sa.update(web_sessions)
                .values(
                    state=_SESSION_STATE_REVOKED,
                    revoked_at=command.database_now,
                    revocation_reason=EMERGENCY_RESET_REVOCATION_REASON,
                    authenticated_at=None,
                    reauthenticated_at=None,
                )
                .where(
                    web_sessions.c.user_id == identity.user_id,
                    web_sessions.c.state != _SESSION_STATE_REVOKED,
                )
            )
            revoked_devices = await connection.execute(
                sa.update(devices)
                .values(status=_DEVICE_STATUS_REVOKED, revoked_at=command.database_now)
                .where(
                    devices.c.user_id == identity.user_id,
                    devices.c.status == _DEVICE_STATUS_ACTIVE,
                )
            )
            revoked_families = await connection.execute(
                sa.update(device_token_families)
                .values(
                    state=_TOKEN_FAMILY_STATE_REVOKED,
                    revoked_at=command.database_now,
                    revocation_reason=EMERGENCY_RESET_REVOCATION_REASON,
                )
                .where(
                    device_token_families.c.user_id == identity.user_id,
                    device_token_families.c.state == _TOKEN_FAMILY_STATE_ACTIVE,
                )
            )
            revoked_tokens = await connection.execute(
                sa.update(device_tokens)
                .values(state=_TOKEN_STATE_REVOKED, revoked_at=command.database_now)
                .where(
                    device_tokens.c.user_id == identity.user_id,
                    device_tokens.c.state == _TOKEN_STATE_ACTIVE,
                )
            )
            denied_grants = await connection.execute(
                sa.update(device_authorization_grants)
                .values(state=_GRANT_STATE_DENIED, denied_at=command.database_now)
                .where(
                    sa.or_(
                        device_authorization_grants.c.state == _GRANT_STATE_PENDING,
                        sa.and_(
                            device_authorization_grants.c.state == _GRANT_STATE_APPROVED,
                            device_authorization_grants.c.approved_by_user_id == identity.user_id,
                        ),
                    )
                )
            )
            await _append_audit_event(
                connection,
                diagnostic_context=command.diagnostic_context,
                workspace_id=credential.workspace_id,
                user_id=identity.user_id,
                action=WEB_AUTHENTICATION_RESET_AUDIT_ACTION,
                result=AUDIT_RESULT_SUCCEEDED,
                reason_code=None,
                occurred_at=command.database_now,
            )
            return ResetWebAuthentication(
                user_id=identity.user_id,
                workspace_id=credential.workspace_id,
                credential_revision=next_credential_revision,
                replaced_totp_credential_count=int(replaced_totp.rowcount),
                disabled_recovery_code_count=int(disabled_recovery.rowcount),
                revoked_web_session_count=int(revoked_sessions.rowcount),
                revoked_device_count=int(revoked_devices.rowcount),
                revoked_token_family_count=int(revoked_families.rowcount),
                revoked_device_token_count=int(revoked_tokens.rowcount),
                denied_grant_count=int(denied_grants.rowcount),
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

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

    async def _record_bucket_failure(
        self,
        connection: AsyncConnection,
        bucket_kind: ThrottleBucketKind,
        bucket_hash: str,
        database_now: Any,
    ) -> ThrottleBucketState:
        """Lock one bucket row and apply the pure failure transition.

        The row lock serialises concurrent failures on the same bucket, so the
        read-modify-write cannot lose a count; a bucket locked by a concurrent
        failure keeps its state (the lock cap holds).
        """
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
        next_state = ThrottleBucketState(
            window_started_at=transition.window_started_at,
            failed_attempt_count=transition.failed_attempt_count,
            locked_until=transition.locked_until,
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
        return next_state

    async def _select_locked_credential(
        self, connection: AsyncConnection, user_id: UUID, workspace_id: UUID
    ) -> Any:
        """Lock the credential row and recheck trust, revision and live TOTP.

        ``FOR UPDATE OF user_credentials`` serialises the login-success and
        password-change rechecks against concurrent failures and changes; the
        joined statuses and the active-TOTP existence read inside the same
        lock decide the transition.
        """
        active_totp_exists = (
            sa.select(sa.literal(True))
            .where(
                totp_credentials.c.user_id == user_id,
                totp_credentials.c.state == _TOTP_STATE_ACTIVE,
            )
            .exists()
            .label("has_active_totp")
        )
        result = await connection.execute(
            sa.select(
                user_credentials.c.credential_revision,
                users.c.status.label("user_status"),
                workspaces.c.status.label("workspace_status"),
                active_totp_exists,
            )
            .select_from(user_credentials)
            .join(users, users.c.user_id == user_credentials.c.user_id)
            .join(
                workspaces,
                sa.and_(
                    workspaces.c.workspace_id == user_credentials.c.workspace_id,
                    workspaces.c.owner_user_id == user_credentials.c.user_id,
                ),
            )
            .where(
                user_credentials.c.user_id == user_id,
                user_credentials.c.workspace_id == workspace_id,
            )
            .with_for_update(of=user_credentials)
        )
        row = result.one_or_none()
        if row is None:
            return None
        if (
            row.user_status != _USER_STATUS_ACTIVE
            or row.workspace_status != _WORKSPACE_STATUS_ACTIVE
        ):
            return None
        return row

    async def _is_account_trusted_locked(
        self, connection: AsyncConnection, user_id: UUID, workspace_id: UUID
    ) -> bool:
        """Recheck the trusted account boundary under the credential lock."""
        row = await self._select_locked_credential(connection, user_id, workspace_id)
        return row is not None


async def _select_locked_canonical_identity(connection: AsyncConnection, username: str) -> Any:
    """Lock the canonical user row and resolve exactly one active workspace.

    ``FOR UPDATE OF users`` serialises concurrent enrollments and emergency
    resets of the same canonical username, so every decision those protected
    transactions take afterwards — the existing-credential refusal, the
    revision bump, the surface revocations — happens under the identity lock.
    The active-workspace outer join keeps one row per identity: a missing or
    archived workspace leaves ``workspace_id`` null and refuses the operation.
    """
    result = await connection.execute(
        sa.select(
            users.c.user_id,
            users.c.status.label("user_status"),
            workspaces.c.workspace_id,
        )
        .select_from(users)
        .outerjoin(
            workspaces,
            sa.and_(
                workspaces.c.owner_user_id == users.c.user_id,
                workspaces.c.status == _WORKSPACE_STATUS_ACTIVE,
            ),
        )
        .where(users.c.username == username)
        .with_for_update(of=users)
    )
    rows = result.all()
    if len(rows) != 1 or rows[0].user_status != _USER_STATUS_ACTIVE or rows[0].workspace_id is None:
        return None
    return rows[0]


async def _append_audit_event(
    connection: AsyncConnection,
    *,
    diagnostic_context: DiagnosticContext,
    workspace_id: UUID,
    user_id: UUID,
    action: str,
    result: str,
    reason_code: str | None,
    occurred_at: Any,
) -> None:
    """Insert one append-only trusted-account audit row (spec 21)."""
    await connection.execute(
        sa.insert(audit_events).values(
            audit_event_id=uuid7(),
            workspace_id=workspace_id,
            actor_kind=AUDIT_ACTOR_KIND_USER,
            actor_id=user_id,
            actor_reference=None,
            action=action,
            target_kind=AUDIT_TARGET_KIND_USER_CREDENTIAL,
            target_id=user_id,
            request_id=diagnostic_context.request_id,
            client_request_id=diagnostic_context.client_request_id,
            trace_id=diagnostic_context.trace.trace_id.value,
            result=result,
            reason_code=reason_code,
            safe_diff_hash=None,
            occurred_at=occurred_at,
        )
    )
