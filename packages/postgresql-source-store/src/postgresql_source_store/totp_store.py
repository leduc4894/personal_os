"""TOTP-anchored enrollment, verification, recovery and disable transactions.

:class:`TotpStore` implements the ``TotpTransactionPort`` over the migrated
authentication schema. Every method is exactly one transaction with one
commit: ``verify_totp`` locks the active credential row ``FOR UPDATE``,
decrypts through the codec seam, accepts only a time step strictly newer than
``last_accepted_time_step`` and — when the row still references a previous
key — re-encrypts the secret with the current key under the same lock before
commit (spec 20.1); ``insert_pending_enrollment`` locks the credential row,
refuses an existing active credential unless the replacement path drives it
and supersedes any stale pending row; ``activate_enrollment`` flips the
pending row to active, replaces the previous active credential, inserts the
ten hashed recovery rows and, for the recovery completion, rotates the
``recovery_limited`` binding to active inside the same commit;
``recover_session`` consumes exactly one unused code under the credential and
code row locks while rotating the presented binding into ``recovery_limited``
without touching its expiry bounds; ``regenerate_recovery_codes`` bumps the
credential's recovery revision and marks the unused prior revision used; and
``disable_totp`` replaces the credential, revokes every recovery code, bumps
the credential revision, revokes the other Web sessions and rotates the
current session to password-only authentication.

Hashing and secret generation stay with the caller outside the transactions;
only the mandated previous-key re-encryption calls back into the codec while
the row lock is held. Every statement is schema-qualified through the Task 6
Core metadata and parameter-bound; transactions run through the shared
bounded-contention runner.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.authentication.contracts import WebSessionState
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import (
    ACTIVE_SESSION_IDLE_TTL,
    ThrottleBucketKind,
    ThrottleBucketState,
    ThrottleFailureTransition,
    ThrottleWindowPolicy,
)
from personal_os.authentication.totp import (
    RECOVERY_AUTHENTICATION_METHOD,
    TOTP_ALGORITHM,
    TOTP_AUTHENTICATION_METHOD,
    TOTP_DIGITS,
    TOTP_DISABLED_REVOCATION_REASON,
    TOTP_PERIOD_SECONDS,
    ActivatedTotpEnrollment,
    ActivateEnrollmentCommand,
    DisabledTotp,
    DisableTotpCommand,
    InsertedPendingEnrollment,
    InsertPendingEnrollmentCommand,
    RecoveredSession,
    RecoverSessionCommand,
    RegeneratedRecoveryCodes,
    RegenerateRecoveryCodesCommand,
    SealedTotpSecret,
    TotpSecretCodecPort,
    TotpVerified,
    VerifyTotpCommand,
    resolve_totp_step,
)
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.authentication_credentials import (
    AUDIT_ACTOR_KIND_USER,
    AUDIT_RESULT_SUCCEEDED,
    AUDIT_TARGET_KIND_USER_CREDENTIAL,
    apply_throttle_bucket_failure,
    run_authentication_transaction,
)
from postgresql_source_store.tables import (
    audit_events,
    authentication_throttle_buckets,
    totp_credentials,
    totp_recovery_codes,
    user_credentials,
    users,
    web_sessions,
)

#: Lifecycle states referenced by the lookups and rechecks.
_TOTP_STATE_ACTIVE: Final[str] = "active"
_TOTP_STATE_PENDING: Final[str] = "pending"
_TOTP_STATE_REPLACED: Final[str] = "replaced"
_SESSION_STATE_ACTIVE: Final[str] = "active"

#: Audit action tokens of the TOTP transitions (spec 21).
TOTP_ENROLLMENT_STARTED_AUDIT_ACTION: Final[str] = "authentication.totp_enrollment_started"
TOTP_ACTIVATED_AUDIT_ACTION: Final[str] = "authentication.totp_activated"
TOTP_RECOVERY_CODE_USED_AUDIT_ACTION: Final[str] = "authentication.totp_recovery_code_used"
TOTP_RECOVERY_CODES_REGENERATED_AUDIT_ACTION: Final[str] = (
    "authentication.totp_recovery_codes_regenerated"
)
TOTP_DISABLED_AUDIT_ACTION: Final[str] = "authentication.totp_disabled"

#: Every ``totp_credentials`` column the typed verification path reads.
_TOTP_ROW_COLUMNS: Final[tuple[Any, ...]] = (
    totp_credentials.c.totp_credential_id,
    totp_credentials.c.user_id,
    totp_credentials.c.workspace_id,
    totp_credentials.c.state,
    totp_credentials.c.secret_ciphertext,
    totp_credentials.c.secret_nonce,
    totp_credentials.c.key_id,
    totp_credentials.c.algorithm,
    totp_credentials.c.digits,
    totp_credentials.c.period_seconds,
    totp_credentials.c.last_accepted_time_step,
    totp_credentials.c.enrollment_expires_at,
    totp_credentials.c.revision,
    totp_credentials.c.created_at,
    totp_credentials.c.activated_at,
    totp_credentials.c.replaced_at,
)


class TotpStore:
    """TOTP transactions over the canonical engine.

    The store takes the composition-owned :class:`AsyncEngine` and the
    composition-owned secret codec; it opens no connection at construction.
    Every persisted timestamp is the caller-provided single ``database_now``
    of its service invocation.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        secret_codec: TotpSecretCodecPort,
        throttle_policy: ThrottleWindowPolicy | None = None,
    ) -> None:
        self._engine = engine
        self._secret_codec = secret_codec
        self._throttle_policy = (
            throttle_policy if throttle_policy is not None else ThrottleWindowPolicy()
        )

    # -- throttle buckets ------------------------------------------------------------

    async def resolve_verification_bucket(
        self, *, bucket_kind: ThrottleBucketKind, bucket_hash: str
    ) -> ThrottleBucketState | None:
        """Read one verification bucket's counting state lock-free."""

        async def operation(connection: AsyncConnection) -> ThrottleBucketState | None:
            return await self._select_bucket(connection, bucket_kind, bucket_hash)

        return await run_authentication_transaction(self._engine, operation)

    async def record_verification_failure(
        self, *, bucket_kind: ThrottleBucketKind, bucket_hash: str, database_now: datetime
    ) -> ThrottleFailureTransition:
        """Lock one bucket row and apply the pure failure transition."""

        async def operation(connection: AsyncConnection) -> ThrottleFailureTransition:
            return await self._record_bucket_failure(
                connection, bucket_kind, bucket_hash, database_now
            )

        return await run_authentication_transaction(self._engine, operation)

    # -- lock-free state ---------------------------------------------------------------

    async def has_active_totp(self, *, user_id: UUID) -> bool:
        """Whether the user carries an active TOTP credential (spec 9.4)."""
        statement = (
            sa.select(totp_credentials.c.totp_credential_id)
            .where(
                totp_credentials.c.user_id == user_id,
                totp_credentials.c.state == _TOTP_STATE_ACTIVE,
            )
            .limit(1)
        )

        async def operation(connection: AsyncConnection) -> bool:
            return (await connection.execute(statement)).one_or_none() is not None

        return await run_authentication_transaction(self._engine, operation)

    async def record_prompt_dismissal(
        self, *, user_id: UUID, workspace_id: UUID, database_now: datetime
    ) -> datetime:
        """Record the skippable-offer dismissal on the credential row (10.1)."""

        async def operation(connection: AsyncConnection) -> datetime:
            locked = await connection.execute(
                sa.select(user_credentials.c.user_id)
                .where(
                    user_credentials.c.user_id == user_id,
                    user_credentials.c.workspace_id == workspace_id,
                )
                .with_for_update(of=user_credentials)
            )
            if locked.one_or_none() is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            updated = await connection.execute(
                sa.update(user_credentials)
                .values(totp_prompt_dismissed_at=database_now, updated_at=database_now)
                .where(user_credentials.c.user_id == user_id)
            )
            if updated.rowcount != 1:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            return database_now

        return await run_authentication_transaction(self._engine, operation)

    # -- enrollment ---------------------------------------------------------------------

    async def insert_pending_enrollment(
        self, command: InsertPendingEnrollmentCommand
    ) -> InsertedPendingEnrollment:
        """Insert one pending enrollment row behind the credential lock."""

        async def operation(connection: AsyncConnection) -> InsertedPendingEnrollment:
            credential = await connection.execute(
                sa.select(
                    user_credentials.c.workspace_id,
                    user_credentials.c.credential_revision,
                    users.c.username,
                )
                .select_from(user_credentials)
                .join(users, users.c.user_id == user_credentials.c.user_id)
                .where(user_credentials.c.user_id == command.user_id)
                .with_for_update(of=user_credentials)
            )
            credential_row = credential.one_or_none()
            if credential_row is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            active_exists = await connection.execute(
                sa.select(totp_credentials.c.totp_credential_id)
                .where(
                    totp_credentials.c.user_id == command.user_id,
                    totp_credentials.c.state == _TOTP_STATE_ACTIVE,
                )
                .limit(1)
            )
            if active_exists.one_or_none() is not None and not command.allow_active_credential:
                raise AuthenticationError(ErrorCode.TOTP_ENROLLMENT_STATE_INVALID)
            await connection.execute(
                sa.update(totp_credentials)
                .values(
                    state=_TOTP_STATE_REPLACED,
                    replaced_at=command.database_now,
                    enrollment_expires_at=None,
                )
                .where(
                    totp_credentials.c.user_id == command.user_id,
                    totp_credentials.c.state == _TOTP_STATE_PENDING,
                )
            )
            totp_credential_id = uuid7()
            await connection.execute(
                sa.insert(totp_credentials).values(
                    totp_credential_id=totp_credential_id,
                    user_id=command.user_id,
                    workspace_id=credential_row.workspace_id,
                    state=_TOTP_STATE_PENDING,
                    secret_ciphertext=command.sealed_secret.ciphertext,
                    secret_nonce=command.sealed_secret.nonce,
                    key_id=command.sealed_secret.key_id,
                    algorithm=TOTP_ALGORITHM,
                    digits=TOTP_DIGITS,
                    period_seconds=TOTP_PERIOD_SECONDS,
                    last_accepted_time_step=None,
                    enrollment_expires_at=command.enrollment_expires_at,
                    revision=1,
                    created_at=command.database_now,
                    activated_at=None,
                    replaced_at=None,
                )
            )
            await _append_audit_event(
                connection,
                diagnostic_context=command.diagnostic_context,
                workspace_id=credential_row.workspace_id,
                user_id=command.user_id,
                action=TOTP_ENROLLMENT_STARTED_AUDIT_ACTION,
                occurred_at=command.database_now,
            )
            return InsertedPendingEnrollment(
                totp_credential_id=totp_credential_id,
                enrollment_expires_at=command.enrollment_expires_at,
                username=credential_row.username,
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    async def activate_enrollment(
        self, command: ActivateEnrollmentCommand
    ) -> ActivatedTotpEnrollment:
        """Verify one enrollment code and activate atomically (spec 10.1)."""

        async def operation(connection: AsyncConnection) -> ActivatedTotpEnrollment:
            locked = await connection.execute(
                sa.select(*_TOTP_ROW_COLUMNS)
                .where(
                    totp_credentials.c.totp_credential_id == command.enrollment_id,
                    totp_credentials.c.user_id == command.user_id,
                )
                .with_for_update(of=totp_credentials)
            )
            row = locked.one_or_none()
            if (
                row is None
                or row.state != _TOTP_STATE_PENDING
                or row.enrollment_expires_at is None
                or row.enrollment_expires_at <= command.database_now
            ):
                raise AuthenticationError(ErrorCode.TOTP_ENROLLMENT_STATE_INVALID)
            secret = self._secret_codec.open_secret(
                sealed=SealedTotpSecret(
                    key_id=row.key_id,
                    nonce=row.secret_nonce,
                    ciphertext=row.secret_ciphertext,
                )
            )
            accepted_step = resolve_totp_step(
                submitted_code=command.submitted_code,
                secret=secret,
                last_accepted_time_step=row.last_accepted_time_step,
                unix_time_seconds=command.unix_time_seconds,
                digits=int(row.digits),
                period_seconds=int(row.period_seconds),
            )
            if accepted_step is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            replaced_previous = await connection.execute(
                sa.update(totp_credentials)
                .values(
                    state=_TOTP_STATE_REPLACED,
                    replaced_at=command.database_now,
                    enrollment_expires_at=None,
                )
                .where(
                    totp_credentials.c.user_id == command.user_id,
                    totp_credentials.c.state == _TOTP_STATE_ACTIVE,
                )
            )
            activated = await connection.execute(
                sa.update(totp_credentials)
                .values(
                    state=_TOTP_STATE_ACTIVE,
                    activated_at=command.database_now,
                    enrollment_expires_at=None,
                    last_accepted_time_step=accepted_step,
                )
                .where(
                    totp_credentials.c.totp_credential_id == command.enrollment_id,
                    totp_credentials.c.state == _TOTP_STATE_PENDING,
                )
            )
            if activated.rowcount != 1:
                raise AuthenticationError(ErrorCode.TOTP_ENROLLMENT_STATE_INVALID)
            for code_hash in command.recovery_code_hashes:
                await connection.execute(
                    sa.insert(totp_recovery_codes).values(
                        recovery_code_id=uuid7(),
                        totp_credential_id=command.enrollment_id,
                        user_id=command.user_id,
                        workspace_id=row.workspace_id,
                        revision=int(row.revision),
                        code_hash=code_hash,
                        created_at=command.database_now,
                    )
                )
            if command.complete_recovery_session:
                rotated = await connection.execute(
                    sa.update(web_sessions)
                    .values(
                        state=_SESSION_STATE_ACTIVE,
                        authentication_method=TOTP_AUTHENTICATION_METHOD,
                        session_secret_hash=command.new_session_secret_hash,
                        csrf_secret_hash=command.new_csrf_secret_hash,
                        authenticated_at=command.database_now,
                        reauthenticated_at=None,
                        # The activated idle window never passes the binding's
                        # absolute boundary (spec 9.2).
                        idle_expires_at=sa.func.least(
                            command.database_now + ACTIVE_SESSION_IDLE_TTL,
                            web_sessions.c.absolute_expires_at,
                        ),
                    )
                    .where(
                        web_sessions.c.web_session_id == command.current_web_session_id,
                        web_sessions.c.session_secret_hash == command.prior_session_secret_hash,
                        web_sessions.c.state == WebSessionState.RECOVERY_LIMITED.value,
                    )
                )
                if rotated.rowcount != 1:
                    raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            await _append_audit_event(
                connection,
                diagnostic_context=command.diagnostic_context,
                workspace_id=row.workspace_id,
                user_id=command.user_id,
                action=TOTP_ACTIVATED_AUDIT_ACTION,
                occurred_at=command.database_now,
            )
            return ActivatedTotpEnrollment(
                totp_credential_id=command.enrollment_id,
                recovery_code_revision=int(row.revision),
                replaced_previous_credential=int(replaced_previous.rowcount) == 1,
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    # -- verification ----------------------------------------------------------------------

    async def verify_totp(self, command: VerifyTotpCommand) -> TotpVerified:
        """Replay-locked verification of one submitted code (spec 10.2)."""

        async def operation(connection: AsyncConnection) -> TotpVerified:
            locked = await connection.execute(self._active_credential_statement(command.user_id))
            row = locked.one_or_none()
            if row is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            secret = self._secret_codec.open_secret(
                sealed=SealedTotpSecret(
                    key_id=row.key_id,
                    nonce=row.secret_nonce,
                    ciphertext=row.secret_ciphertext,
                )
            )
            accepted_step = resolve_totp_step(
                submitted_code=command.submitted_code,
                secret=secret,
                last_accepted_time_step=row.last_accepted_time_step,
                unix_time_seconds=command.unix_time_seconds,
                digits=int(row.digits),
                period_seconds=int(row.period_seconds),
            )
            if accepted_step is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            values: dict[str, Any] = {"last_accepted_time_step": accepted_step}
            was_reencrypted = False
            if row.key_id != self._secret_codec.current_key_id():
                # Spec 20.1: re-encrypt under the current key while the row
                # lock is held, before the marker advance commits.
                resealed = self._secret_codec.seal_secret(plaintext=secret)
                values["secret_nonce"] = resealed.nonce
                values["secret_ciphertext"] = resealed.ciphertext
                values["key_id"] = resealed.key_id
                was_reencrypted = True
            updated = await connection.execute(
                sa.update(totp_credentials)
                .values(**values)
                .where(
                    totp_credentials.c.totp_credential_id == row.totp_credential_id,
                    totp_credentials.c.state == _TOTP_STATE_ACTIVE,
                )
            )
            if updated.rowcount != 1:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            if command.reset_bucket_hash is not None:
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
                        == ThrottleBucketKind.TOTP_VERIFICATION.value,
                        authentication_throttle_buckets.c.bucket_hash == command.reset_bucket_hash,
                    )
                )
            return TotpVerified(
                totp_credential_id=row.totp_credential_id,
                accepted_time_step=accepted_step,
                was_reencrypted=was_reencrypted,
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    # -- recovery --------------------------------------------------------------------------

    async def recover_session(self, command: RecoverSessionCommand) -> RecoveredSession:
        """Consume exactly one unused code and enter ``recovery_limited``."""

        async def operation(connection: AsyncConnection) -> RecoveredSession:
            credential = await connection.execute(
                self._active_credential_statement(command.user_id)
            )
            credential_row = credential.one_or_none()
            if credential_row is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            matching = await connection.execute(
                sa.select(totp_recovery_codes.c.recovery_code_id)
                .where(
                    totp_recovery_codes.c.totp_credential_id == credential_row.totp_credential_id,
                    totp_recovery_codes.c.code_hash == command.recovery_code_hash,
                    totp_recovery_codes.c.used_at.is_(None),
                )
                .limit(1)
                .with_for_update(of=totp_recovery_codes)
            )
            code_row = matching.one_or_none()
            if code_row is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            consumed = await connection.execute(
                sa.update(totp_recovery_codes)
                .values(used_at=command.database_now)
                .where(
                    totp_recovery_codes.c.recovery_code_id == code_row.recovery_code_id,
                    totp_recovery_codes.c.used_at.is_(None),
                )
            )
            if consumed.rowcount != 1:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            rotated = await connection.execute(
                sa.update(web_sessions)
                .values(
                    state=WebSessionState.RECOVERY_LIMITED.value,
                    authentication_method=RECOVERY_AUTHENTICATION_METHOD,
                    session_secret_hash=command.new_session_secret_hash,
                    csrf_secret_hash=command.new_csrf_secret_hash,
                    authenticated_at=command.database_now,
                    reauthenticated_at=None,
                )
                .where(
                    web_sessions.c.web_session_id == command.current_web_session_id,
                    web_sessions.c.session_secret_hash == command.prior_session_secret_hash,
                    web_sessions.c.state.in_(
                        (
                            WebSessionState.PENDING_TOTP.value,
                            WebSessionState.ACTIVE.value,
                        )
                    ),
                )
            )
            if rotated.rowcount != 1:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            await _append_audit_event(
                connection,
                diagnostic_context=command.diagnostic_context,
                workspace_id=credential_row.workspace_id,
                user_id=command.user_id,
                action=TOTP_RECOVERY_CODE_USED_AUDIT_ACTION,
                occurred_at=command.database_now,
            )
            return RecoveredSession(
                web_session_id=command.current_web_session_id,
                state=WebSessionState.RECOVERY_LIMITED,
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    # -- regeneration and disable ------------------------------------------------------------

    async def regenerate_recovery_codes(
        self, command: RegenerateRecoveryCodesCommand
    ) -> RegeneratedRecoveryCodes:
        """Invalidate the unused prior revision and insert a fresh one."""

        async def operation(connection: AsyncConnection) -> RegeneratedRecoveryCodes:
            credential = await connection.execute(
                self._active_credential_statement(command.user_id)
            )
            credential_row = credential.one_or_none()
            if credential_row is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            next_revision = int(credential_row.revision) + 1
            await connection.execute(
                sa.update(totp_credentials)
                .values(revision=next_revision)
                .where(totp_credentials.c.totp_credential_id == credential_row.totp_credential_id)
            )
            invalidated = await connection.execute(
                sa.update(totp_recovery_codes)
                .values(used_at=command.database_now)
                .where(
                    totp_recovery_codes.c.totp_credential_id == credential_row.totp_credential_id,
                    totp_recovery_codes.c.used_at.is_(None),
                )
            )
            for code_hash in command.recovery_code_hashes:
                await connection.execute(
                    sa.insert(totp_recovery_codes).values(
                        recovery_code_id=uuid7(),
                        totp_credential_id=credential_row.totp_credential_id,
                        user_id=command.user_id,
                        workspace_id=command.workspace_id,
                        revision=next_revision,
                        code_hash=code_hash,
                        created_at=command.database_now,
                    )
                )
            await _append_audit_event(
                connection,
                diagnostic_context=command.diagnostic_context,
                workspace_id=command.workspace_id,
                user_id=command.user_id,
                action=TOTP_RECOVERY_CODES_REGENERATED_AUDIT_ACTION,
                occurred_at=command.database_now,
            )
            return RegeneratedRecoveryCodes(
                revision=next_revision,
                invalidated_code_count=int(invalidated.rowcount),
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    async def disable_totp(self, command: DisableTotpCommand) -> DisabledTotp:
        """Close every TOTP surface and rotate the current session (10.3)."""

        async def operation(connection: AsyncConnection) -> DisabledTotp:
            locked_credential = await connection.execute(
                sa.select(user_credentials.c.workspace_id, user_credentials.c.credential_revision)
                .where(
                    user_credentials.c.user_id == command.user_id,
                    user_credentials.c.workspace_id == command.workspace_id,
                )
                .with_for_update(of=user_credentials)
            )
            credential_row = locked_credential.one_or_none()
            if credential_row is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            active_credential = await connection.execute(
                self._active_credential_statement(command.user_id)
            )
            if active_credential.one_or_none() is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            next_credential_revision = int(credential_row.credential_revision) + 1
            await connection.execute(
                sa.update(totp_credentials)
                .values(
                    state=_TOTP_STATE_REPLACED,
                    replaced_at=command.database_now,
                    enrollment_expires_at=None,
                )
                .where(
                    totp_credentials.c.user_id == command.user_id,
                    totp_credentials.c.state.in_((_TOTP_STATE_ACTIVE, _TOTP_STATE_PENDING)),
                )
            )
            await connection.execute(
                sa.update(totp_recovery_codes)
                .values(used_at=command.database_now)
                .where(
                    totp_recovery_codes.c.user_id == command.user_id,
                    totp_recovery_codes.c.used_at.is_(None),
                )
            )
            await connection.execute(
                sa.update(user_credentials)
                .values(
                    credential_revision=next_credential_revision,
                    updated_at=command.database_now,
                )
                .where(user_credentials.c.user_id == command.user_id)
            )
            revoked = await connection.execute(
                sa.update(web_sessions)
                .values(
                    state=WebSessionState.REVOKED.value,
                    revoked_at=command.database_now,
                    revocation_reason=TOTP_DISABLED_REVOCATION_REASON,
                    authenticated_at=None,
                    reauthenticated_at=None,
                )
                .where(
                    web_sessions.c.user_id == command.user_id,
                    web_sessions.c.web_session_id != command.current_web_session_id,
                    web_sessions.c.state != WebSessionState.REVOKED.value,
                )
            )
            rotated = await connection.execute(
                sa.update(web_sessions)
                .values(
                    session_secret_hash=command.new_session_secret_hash,
                    csrf_secret_hash=command.new_csrf_secret_hash,
                    credential_revision=next_credential_revision,
                    authentication_method="password",
                )
                .where(
                    web_sessions.c.web_session_id == command.current_web_session_id,
                    web_sessions.c.user_id == command.user_id,
                    web_sessions.c.session_secret_hash == command.prior_session_secret_hash,
                    web_sessions.c.state != WebSessionState.REVOKED.value,
                )
            )
            if rotated.rowcount != 1:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            await _append_audit_event(
                connection,
                diagnostic_context=command.diagnostic_context,
                workspace_id=command.workspace_id,
                user_id=command.user_id,
                action=TOTP_DISABLED_AUDIT_ACTION,
                occurred_at=command.database_now,
            )
            return DisabledTotp(
                credential_revision=next_credential_revision,
                revoked_session_count=int(revoked.rowcount),
                database_now=command.database_now,
            )

        return await run_authentication_transaction(self._engine, operation)

    # -- internal helpers -------------------------------------------------

    def _active_credential_statement(self, user_id: UUID) -> sa.Select[tuple[Any, ...]]:
        return (
            sa.select(*_TOTP_ROW_COLUMNS)
            .where(
                totp_credentials.c.user_id == user_id,
                totp_credentials.c.state == _TOTP_STATE_ACTIVE,
            )
            .with_for_update(of=totp_credentials)
        )

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
        database_now: datetime,
    ) -> ThrottleFailureTransition:
        """Lock one bucket row and apply the pure failure transition.

        Delegates to :func:`apply_throttle_bucket_failure`, whose guarded cold
        insert closes the concurrent first-failure race on one bucket.
        """
        return await apply_throttle_bucket_failure(
            connection,
            bucket_kind=bucket_kind,
            bucket_hash=bucket_hash,
            database_now=database_now,
            policy=self._throttle_policy,
        )


async def _append_audit_event(
    connection: AsyncConnection,
    *,
    diagnostic_context: DiagnosticContext,
    workspace_id: UUID,
    user_id: UUID,
    action: str,
    occurred_at: datetime,
) -> None:
    """Insert one append-only TOTP transition audit row (spec 21)."""
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
            result=AUDIT_RESULT_SUCCEEDED,
            reason_code=None,
            safe_diff_hash=None,
            occurred_at=occurred_at,
        )
    )
