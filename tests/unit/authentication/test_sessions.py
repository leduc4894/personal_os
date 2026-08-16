"""Password-login, session and password-change transitions over port doubles.

The services under test own the closed authentication choreography of spec 8
and 9: unknown user and wrong password are indistinguishable in public error
and hasher work; the fifth failure locks both HMACed buckets for exactly
fifteen minutes; a locked bucket rejects before any verifier work; success
resets the credential streak and starts an ``active`` or ``pending_totp``
session whose stored hashes never persist the secrets themselves; session
authentication enforces state, credential revision and both expiry windows;
re-authentication and password change rotate the session and CSRF binding; and
password hashing plus secret generation always happen outside the database
transactions. The store, hasher, crypto and clock ports are in-memory doubles
asserting the call contracts, never a live database.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from personal_os.authentication.contracts import (
    AUTHENTICATED_WEB_SCOPES,
    WebScope,
    WebSessionState,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.passwords import PasswordBlocklist
from personal_os.authentication.sessions import (
    DUMMY_LOGIN_PHC_HASH,
    LOGIN_FAILURE_THRESHOLD,
    ChangedPassword,
    ChangePasswordCommand,
    CommitLoginSuccessCommand,
    CommittedLoginSuccess,
    LoginService,
    PasswordChangeService,
    RecordedLoginFailure,
    RecordLoginFailureCommand,
    ResolvedLoginMaterial,
    ResolvedWebSession,
    RevokedWebSession,
    RevokeWebSessionCommand,
    RotatedWebSessionSecrets,
    RotateWebSessionSecretsCommand,
    SessionRotationCause,
    SessionService,
    SessionWindowPolicy,
    StoredWebSession,
    ThrottleBucketKind,
    ThrottleBucketState,
    ThrottleWindowPolicy,
    evaluate_session_authentication,
    is_recently_authenticated,
    is_throttle_bucket_locked,
    next_login_failure_transition,
    recent_authentication_moment,
    successful_authentication_reset,
)
from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode

_DATABASE_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_USERNAME = "owner"
_SOURCE_BUCKET = "203.0.113.7"
_CORRECT_PASSWORD = "correct-horse-battery-staple"
_WRONG_PASSWORD = "sentinel-wrong-password-value"
_MASTER_KEY = bytes(range(32))


def _diagnostic_context() -> Any:
    return create_diagnostic_context().context


class CountingPasswordHasher:
    """Hasher double counting verifier calls and scripting rehash upgrades."""

    def __init__(self, *, valid_password: str, needs_rehash: bool = False) -> None:
        self._valid_password = valid_password
        self._needs_rehash = needs_rehash
        self.verify_calls = 0
        self.verify_arguments: list[tuple[str, str]] = []
        self.hashed_passwords: list[str] = []

    def hash_password(self, password: str) -> str:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]
        encoded_hash = f"$argon2id$v=19$m=65536,t=3,p=1${digest}$rehashedsecretvalue"
        self.hashed_passwords.append(encoded_hash)
        return encoded_hash

    def verify_password(self, password_hash: str, password: str) -> bool:
        self.verify_calls += 1
        self.verify_arguments.append((password_hash, password))
        return password == self._valid_password

    def needs_rehash(self, password_hash: str) -> bool:
        return self._needs_rehash


class DeterministicCrypto:
    """Crypto double deriving stable subkeys and real stdlib HMAC digests."""

    def derive_subkey(self, *, master_key: bytes, label: str) -> bytes:
        return hashlib.sha256(label.encode("ascii") + master_key).digest()

    def hmac_sha256(self, *, key: bytes, message: bytes) -> bytes:
        return hmac.new(key, message, hashlib.sha256).digest()

    def seal_secret(self, *, key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
        raise AssertionError("sealing is outside the login/session transitions")

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
        raise AssertionError("opening is outside the login/session transitions")


class FixedClock:
    """Clock double returning one controllable transaction timestamp."""

    def __init__(self, database_now: datetime = _DATABASE_NOW) -> None:
        self.database_now_value = database_now

    async def database_now(self) -> datetime:
        return self.database_now_value


class AdvancingClock(FixedClock):
    """Clock double consuming scripted reads in order and counting them.

    The first read returns the first scripted moment, each further read
    advances to the next; reads beyond the script keep returning the last
    value. ``read_count`` proves how many clock reads one invocation took.
    """

    def __init__(self, reads: tuple[datetime, ...]) -> None:
        super().__init__(reads[0])
        self._pending_reads = list(reads[1:])
        self.read_count = 0

    async def database_now(self) -> datetime:
        self.read_count += 1
        if self.read_count > 1 and self._pending_reads:
            self.database_now_value = self._pending_reads.pop(0)
        return self.database_now_value


class FakeCredentialTransactions:
    """In-memory credential/throttle port double applying the domain rules."""

    def __init__(
        self,
        *,
        throttle_policy: ThrottleWindowPolicy | None = None,
    ) -> None:
        self.throttle_policy = (
            throttle_policy if throttle_policy is not None else ThrottleWindowPolicy()
        )
        self.buckets: dict[tuple[str, str], ThrottleBucketState] = {}
        self.materials: dict[str, ResolvedLoginMaterial] = {}
        self.password_hashes: dict[UUID, str] = {}
        self.has_active_totp = False
        self.session_store: FakeWebSessionTransactions | None = None
        self.failure_commands: list[RecordLoginFailureCommand] = []
        self.success_commands: list[CommitLoginSuccessCommand] = []
        self.change_commands: list[ChangePasswordCommand] = []

    def bind_session_store(self, session_store: FakeWebSessionTransactions) -> None:
        """Mirror the real schema: the success transaction writes the row."""
        self.session_store = session_store

    def enroll_trusted_account(
        self,
        *,
        username: str = _USERNAME,
        credential_revision: int = 1,
        password_hash: str | None = None,
    ) -> None:
        resolved_password_hash = (
            password_hash if password_hash is not None else DUMMY_LOGIN_PHC_HASH
        )
        material = ResolvedLoginMaterial(
            user_id=uuid4(),
            workspace_id=uuid4(),
            is_trusted_account=True,
            password_hash=resolved_password_hash,
            credential_revision=credential_revision,
            username_bucket=None,
            source_bucket=None,
        )
        self.materials[username] = material
        self.password_hashes[material.user_id] = resolved_password_hash

    async def resolve_login_material(
        self, *, username: str, username_bucket_hash: str, source_bucket_hash: str
    ) -> ResolvedLoginMaterial:
        material = self.materials.get(username)
        if material is None:
            return ResolvedLoginMaterial(
                user_id=None,
                workspace_id=None,
                is_trusted_account=False,
                password_hash=None,
                credential_revision=None,
                username_bucket=None,
                source_bucket=None,
            )
        return replace(
            material,
            username_bucket=self.buckets.get(
                (ThrottleBucketKind.LOGIN_USERNAME.value, username_bucket_hash)
            ),
            source_bucket=self.buckets.get(
                (ThrottleBucketKind.LOGIN_SOURCE.value, source_bucket_hash)
            ),
        )

    async def record_login_failure(
        self, command: RecordLoginFailureCommand
    ) -> RecordedLoginFailure:
        self.failure_commands.append(command)
        for bucket_kind, bucket_hash in (
            (ThrottleBucketKind.LOGIN_USERNAME.value, command.username_bucket_hash),
            (ThrottleBucketKind.LOGIN_SOURCE.value, command.source_bucket_hash),
        ):
            previous = self.buckets.get((bucket_kind, bucket_hash))
            transition = next_login_failure_transition(
                previous, database_now=command.database_now, policy=self.throttle_policy
            )
            self.buckets[(bucket_kind, bucket_hash)] = ThrottleBucketState(
                window_started_at=transition.window_started_at,
                failed_attempt_count=transition.failed_attempt_count,
                locked_until=transition.locked_until,
            )
        return RecordedLoginFailure(
            username_bucket=self.buckets[
                (ThrottleBucketKind.LOGIN_USERNAME.value, command.username_bucket_hash)
            ],
            source_bucket=self.buckets[
                (ThrottleBucketKind.LOGIN_SOURCE.value, command.source_bucket_hash)
            ],
            was_audited=command.user_id is not None,
        )

    async def commit_login_success(
        self, command: CommitLoginSuccessCommand
    ) -> CommittedLoginSuccess:
        self.success_commands.append(command)
        self.buckets[
            (ThrottleBucketKind.LOGIN_USERNAME.value, command.username_bucket_hash)
        ] = successful_authentication_reset(database_now=command.database_now)
        state = WebSessionState.PENDING_TOTP if self.has_active_totp else WebSessionState.ACTIVE
        idle_expires_at = (
            command.pending_totp_idle_expires_at
            if state is WebSessionState.PENDING_TOTP
            else command.active_idle_expires_at
        )
        if self.session_store is not None:
            self.session_store.register(
                StoredWebSession(
                    web_session_id=command.web_session_id,
                    user_id=command.user_id,
                    workspace_id=command.workspace_id,
                    session_secret_hash=command.session_secret_hash,
                    csrf_secret_hash=command.csrf_secret_hash,
                    state=state,
                    credential_revision=command.expected_credential_revision,
                    authentication_method=command.authentication_method,
                    created_at=command.database_now,
                    authenticated_at=(
                        command.database_now
                        if state is WebSessionState.ACTIVE
                        else None
                    ),
                    reauthenticated_at=None,
                    last_seen_at=None,
                    idle_expires_at=idle_expires_at,
                    absolute_expires_at=command.absolute_expires_at,
                    revoked_at=None,
                    revocation_reason=None,
                ),
                current_credential_revision=command.expected_credential_revision,
                password_hash=self.password_hashes.get(
                    command.user_id, DUMMY_LOGIN_PHC_HASH
                ),
            )
        return CommittedLoginSuccess(
            web_session_id=command.web_session_id,
            user_id=command.user_id,
            workspace_id=command.workspace_id,
            state=state,
            credential_revision=command.expected_credential_revision,
            authenticated_at=command.database_now if state is WebSessionState.ACTIVE else None,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=command.absolute_expires_at,
            database_now=command.database_now,
        )

    async def change_password(self, command: ChangePasswordCommand) -> ChangedPassword:
        self.change_commands.append(command)
        next_revision = command.expected_credential_revision + 1
        self.password_hashes[command.user_id] = command.new_password_hash
        revoked_count = 0
        if self.session_store is not None:
            for secret_hash, row in list(self.session_store.rows_by_secret_hash.items()):
                if row.user_id != command.user_id:
                    continue
                if row.web_session_id == command.current_web_session_id:
                    rotated = replace(
                        row,
                        session_secret_hash=command.new_session_secret_hash,
                        csrf_secret_hash=command.new_csrf_secret_hash,
                        credential_revision=next_revision,
                    )
                    del self.session_store.rows_by_secret_hash[secret_hash]
                    self.session_store.rows_by_secret_hash[command.new_session_secret_hash] = (
                        rotated
                    )
                elif row.state is not WebSessionState.REVOKED:
                    revoked_count += 1
                    self.session_store.rows_by_secret_hash[secret_hash] = replace(
                        row,
                        state=WebSessionState.REVOKED,
                        revoked_at=command.database_now,
                        revocation_reason="password_changed",
                        authenticated_at=None,
                        reauthenticated_at=None,
                    )
            self.session_store.current_credential_revisions[command.user_id] = next_revision
            self.session_store.password_hashes[command.user_id] = command.new_password_hash
        return ChangedPassword(
            current_web_session_id=command.current_web_session_id,
            credential_revision=next_revision,
            revoked_session_count=revoked_count,
            database_now=command.database_now,
        )


class FakeWebSessionTransactions:
    """In-memory web-session port double applying the domain decisions."""

    def __init__(self, *, session_policy: SessionWindowPolicy | None = None) -> None:
        self.session_policy = (
            session_policy if session_policy is not None else SessionWindowPolicy()
        )
        self.rows_by_secret_hash: dict[str, StoredWebSession] = {}
        self.current_credential_revisions: dict[UUID, int] = {}
        self.password_hashes: dict[UUID, str] = {}
        self.rotation_commands: list[RotateWebSessionSecretsCommand] = []
        self.revocation_commands: list[RevokeWebSessionCommand] = []

    def register(
        self,
        session: StoredWebSession,
        *,
        current_credential_revision: int,
        password_hash: str = DUMMY_LOGIN_PHC_HASH,
    ) -> None:
        self.rows_by_secret_hash[session.session_secret_hash] = session
        self.current_credential_revisions[session.user_id] = current_credential_revision
        self.password_hashes[session.user_id] = password_hash

    async def resolve_session(
        self, *, session_secret_hash: str, database_now: datetime
    ) -> ResolvedWebSession:
        session = self.rows_by_secret_hash.get(session_secret_hash)
        if session is None:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        decision = evaluate_session_authentication(
            session,
            current_credential_revision=self.current_credential_revisions[session.user_id],
            database_now=database_now,
            policy=self.session_policy,
        )
        if not decision.is_authenticated:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        if decision.should_advance_activity and decision.next_idle_expires_at is not None:
            session = replace(
                session,
                last_seen_at=database_now,
                idle_expires_at=decision.next_idle_expires_at,
            )
            self.rows_by_secret_hash[session_secret_hash] = session
        return ResolvedWebSession(
            session=session,
            current_credential_revision=self.current_credential_revisions[session.user_id],
            password_hash=self.password_hashes[session.user_id],
            database_now=database_now,
        )

    async def rotate_session_secrets(
        self, command: RotateWebSessionSecretsCommand
    ) -> RotatedWebSessionSecrets:
        self.rotation_commands.append(command)
        session = self.rows_by_secret_hash.get(command.prior_session_secret_hash)
        if session is None or session.web_session_id != command.web_session_id:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        if command.cause is SessionRotationCause.RECENT_REAUTHENTICATION:
            rotated = replace(session, reauthenticated_at=command.database_now)
        else:
            rotated = replace(
                session,
                state=WebSessionState.ACTIVE,
                authentication_method=command.target_authentication_method,
                authenticated_at=command.database_now,
                idle_expires_at=min(
                    command.database_now + self.session_policy.idle_ttl,
                    session.absolute_expires_at,
                ),
            )
        rotated = replace(
            rotated,
            session_secret_hash=command.new_session_secret_hash,
            csrf_secret_hash=command.new_csrf_secret_hash,
        )
        del self.rows_by_secret_hash[command.prior_session_secret_hash]
        self.rows_by_secret_hash[command.new_session_secret_hash] = rotated
        return RotatedWebSessionSecrets(
            web_session_id=rotated.web_session_id,
            state=rotated.state,
            database_now=command.database_now,
        )

    async def revoke_session(self, command: RevokeWebSessionCommand) -> RevokedWebSession:
        self.revocation_commands.append(command)
        session = self.rows_by_secret_hash.get(command.session_secret_hash)
        if session is None or session.state is WebSessionState.REVOKED:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        revoked = replace(
            session,
            state=WebSessionState.REVOKED,
            revoked_at=command.database_now,
            revocation_reason=command.revocation_reason,
            authenticated_at=None,
            reauthenticated_at=None,
        )
        self.rows_by_secret_hash[session.session_secret_hash] = revoked
        return RevokedWebSession(
            web_session_id=revoked.web_session_id, revoked_at=command.database_now
        )


class LoginHarness:
    """Composition of the real services over the in-memory port doubles."""

    def __init__(
        self,
        *,
        needs_rehash: bool = False,
        clock: FixedClock | None = None,
    ) -> None:
        self.clock = clock if clock is not None else FixedClock()
        self.hasher = CountingPasswordHasher(
            valid_password=_CORRECT_PASSWORD, needs_rehash=needs_rehash
        )
        self.crypto = DeterministicCrypto()
        self.credentials = FakeCredentialTransactions()
        self.credentials.enroll_trusted_account()
        self.sessions = FakeWebSessionTransactions()
        self.credentials.bind_session_store(self.sessions)
        self.login_service = LoginService(
            credentials=self.credentials,
            hasher=self.hasher,
            crypto=self.crypto,
            master_key=_MASTER_KEY,
            clock=self.clock,
        )
        self.session_service = SessionService(
            sessions=self.sessions,
            hasher=self.hasher,
            crypto=self.crypto,
            master_key=_MASTER_KEY,
            clock=self.clock,
        )
        self.password_service = PasswordChangeService(
            session_service=self.session_service,
            credentials=self.credentials,
            hasher=self.hasher,
            blocklist=PasswordBlocklist(digests=()),
        )

    @property
    def database_now(self) -> datetime:
        return self.clock.database_now_value

    async def login(
        self,
        *,
        username: str = _USERNAME,
        password: str = _CORRECT_PASSWORD,
        source_bucket: str = _SOURCE_BUCKET,
    ) -> Any:
        return await self.login_service.login(
            username=username,
            password=password,
            source_bucket=source_bucket,
            diagnostic_context=_diagnostic_context(),
        )

    async def reject_login(self) -> Any:
        return await self.login(password=_WRONG_PASSWORD)


def _active_session(
    *,
    state: WebSessionState = WebSessionState.ACTIVE,
    credential_revision: int = 1,
    idle_expires_at: datetime | None = None,
    absolute_expires_at: datetime | None = None,
    session_secret: bytes = b"session-secret",
    csrf_secret: bytes = b"csrf-secret",
) -> StoredWebSession:
    return StoredWebSession(
        web_session_id=uuid4(),
        user_id=uuid4(),
        workspace_id=uuid4(),
        session_secret_hash=hashlib.sha256(session_secret).hexdigest(),
        csrf_secret_hash=hashlib.sha256(csrf_secret).hexdigest(),
        state=state,
        credential_revision=credential_revision,
        authentication_method="password",
        created_at=_DATABASE_NOW,
        authenticated_at=None if state is WebSessionState.PENDING_TOTP else _DATABASE_NOW,
        reauthenticated_at=None,
        last_seen_at=None,
        idle_expires_at=(
            idle_expires_at
            if idle_expires_at is not None
            else _DATABASE_NOW + timedelta(hours=12)
        ),
        absolute_expires_at=(
            absolute_expires_at
            if absolute_expires_at is not None
            else _DATABASE_NOW + timedelta(days=7)
        ),
        revoked_at=None,
        revocation_reason=None,
    )


# --- login choreography ---------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_and_wrong_password_both_call_hasher_once() -> None:
    harness = LoginHarness()
    unknown = await harness.login(username="missing", password="sentinel")
    wrong = await harness.login(username="owner", password="sentinel")
    assert unknown.public_error == wrong.public_error == ErrorCode.AUTHENTICATION_FAILED
    assert harness.hasher.verify_calls == 2
    # Both calls verify against the fixed dummy-selected hash so unknown user
    # and wrong password execute identical Argon2id work.
    assert harness.hasher.verify_arguments[0][0] == DUMMY_LOGIN_PHC_HASH
    assert harness.hasher.verify_arguments[1][0] == DUMMY_LOGIN_PHC_HASH


@pytest.mark.asyncio
async def test_fifth_failure_locks_bucket_for_fifteen_minutes() -> None:
    harness = LoginHarness()
    outcomes = [await harness.reject_login() for _ in range(5)]
    assert outcomes[-1].locked_until == harness.database_now + timedelta(minutes=15)
    assert [outcome.public_error for outcome in outcomes] == [
        ErrorCode.AUTHENTICATION_FAILED
    ] * 5
    for outcome in outcomes[:-1]:
        assert outcome.locked_until is None


@pytest.mark.asyncio
async def test_locked_bucket_rejects_before_verifier_work() -> None:
    harness = LoginHarness()
    for _ in range(LOGIN_FAILURE_THRESHOLD):
        await harness.reject_login()
    locked = await harness.login()
    assert locked.public_error is ErrorCode.AUTHENTICATION_RATE_LIMITED
    assert locked.locked_until == harness.database_now + timedelta(minutes=15)
    assert locked.started_session is None
    assert harness.hasher.verify_calls == LOGIN_FAILURE_THRESHOLD


@pytest.mark.asyncio
async def test_failure_records_both_hmaced_buckets_without_raw_values() -> None:
    harness = LoginHarness()
    await harness.reject_login()
    command = harness.credentials.failure_commands[0]
    for bucket_hash in (command.username_bucket_hash, command.source_bucket_hash):
        assert len(bucket_hash) == 64
        assert bucket_hash == bucket_hash.lower()
        # Neither raw username nor raw source material equals the stored hash.
        assert bucket_hash != hashlib.sha256(_USERNAME.encode("utf-8")).hexdigest()
        assert bucket_hash != hashlib.sha256(_SOURCE_BUCKET.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_successful_login_resets_credential_streak_and_starts_active_session() -> None:
    harness = LoginHarness()
    await harness.reject_login()
    await harness.reject_login()
    outcome = await harness.login()
    assert outcome.public_error is None
    assert outcome.locked_until is None
    started = outcome.started_session
    assert started is not None
    assert started.state is WebSessionState.ACTIVE
    assert started.authentication_method == "password"
    assert started.credential_revision == 1
    assert started.idle_expires_at == harness.database_now + timedelta(hours=12)
    assert started.absolute_expires_at == harness.database_now + timedelta(days=7)
    assert started.database_now == harness.database_now
    # PostgreSQL stores only hashes of both secrets, never the secrets.
    assert started.session_secret_hash == hashlib.sha256(
        started.session_secret.encode("utf-8")
    ).hexdigest()
    assert started.csrf_secret_hash != hashlib.sha256(
        started.csrf_secret.encode("utf-8")
    ).hexdigest()
    assert started.session_secret != started.csrf_secret
    username_bucket = harness.credentials.buckets[
        (
            ThrottleBucketKind.LOGIN_USERNAME.value,
            harness.credentials.failure_commands[0].username_bucket_hash,
        )
    ]
    assert username_bucket.failed_attempt_count == 0
    assert username_bucket.locked_until is None


@pytest.mark.asyncio
async def test_login_with_active_totp_starts_pending_totp_session() -> None:
    harness = LoginHarness()
    harness.credentials.has_active_totp = True
    outcome = await harness.login()
    started = outcome.started_session
    assert started is not None
    assert started.state is WebSessionState.PENDING_TOTP
    assert started.idle_expires_at == harness.database_now + timedelta(minutes=5)
    assert started.absolute_expires_at == harness.database_now + timedelta(days=7)


@pytest.mark.asyncio
async def test_obsolete_hash_is_upgraded_outside_the_transaction() -> None:
    harness = LoginHarness(needs_rehash=True)
    outcome = await harness.login()
    assert outcome.public_error is None
    command = harness.credentials.success_commands[0]
    assert command.upgraded_password_hash == harness.hasher.hashed_passwords[0]
    assert harness.hasher.verify_calls == 1
    assert len(harness.hasher.hashed_passwords) == 1


@pytest.mark.asyncio
async def test_invalid_username_grammar_fails_without_database_access() -> None:
    harness = LoginHarness()
    outcome = await harness.login(username="Not-A-Canonical-Username")
    assert outcome.public_error is ErrorCode.AUTHENTICATION_FAILED
    assert harness.hasher.verify_calls == 1
    assert harness.credentials.failure_commands == []


# --- session authentication -----------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_returns_full_web_scope_set_and_advances_idle() -> None:
    harness = LoginHarness()
    outcome = await harness.login()
    started = outcome.started_session
    assert started is not None
    harness.clock.database_now_value = _DATABASE_NOW + timedelta(hours=6)
    authenticated = await harness.session_service.authenticate(
        session_secret=started.session_secret
    )
    assert authenticated.context.scopes == AUTHENTICATED_WEB_SCOPES == frozenset(WebScope)
    assert authenticated.context.credential_revision == 1
    assert authenticated.context.web_session_id == started.web_session_id
    assert authenticated.session.idle_expires_at == harness.database_now + timedelta(hours=12)
    assert authenticated.session.last_seen_at == harness.database_now
    # Idle advance never passes the absolute expiry: a session one hour short
    # of its absolute boundary clamps the slide to that boundary.
    absolute_expiry = _DATABASE_NOW + timedelta(hours=17)
    near_absolute = _active_session(
        absolute_expires_at=absolute_expiry, session_secret=b"near-absolute-secret"
    )
    harness.sessions.register(near_absolute, current_credential_revision=1)
    clamped = await harness.session_service.authenticate(session_secret="near-absolute-secret")
    assert clamped.session.idle_expires_at == absolute_expiry


@pytest.mark.asyncio
async def test_authenticate_rejects_unknown_revoked_stale_and_expired_sessions() -> None:
    harness = LoginHarness()
    with pytest.raises(AuthenticationError) as unknown:
        await harness.session_service.authenticate(session_secret="missing-session-secret")
    assert unknown.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED

    session = _active_session()
    harness.sessions.register(session, current_credential_revision=2)
    with pytest.raises(AuthenticationError) as stale:
        await harness.session_service.authenticate(session_secret="session-secret")
    assert stale.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED

    for expired in (
        _active_session(
            idle_expires_at=_DATABASE_NOW - timedelta(seconds=1), session_secret=b"idle-secret"
        ),
        _active_session(
            absolute_expires_at=_DATABASE_NOW - timedelta(seconds=1),
            session_secret=b"absolute-secret",
        ),
        _active_session(state=WebSessionState.REVOKED, session_secret=b"revoked-secret"),
        _active_session(
            state=WebSessionState.PENDING_TOTP, session_secret=b"pending-secret"
        ),
        _active_session(
            state=WebSessionState.RECOVERY_LIMITED, session_secret=b"recovery-secret"
        ),
    ):
        harness.sessions.register(expired, current_credential_revision=1)
    for secret in ("idle", "absolute", "revoked", "pending", "recovery"):
        with pytest.raises(AuthenticationError) as rejected:
            await harness.session_service.authenticate(session_secret=f"{secret}-secret")
        assert rejected.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED


@pytest.mark.asyncio
async def test_reauthenticate_verifies_password_and_rotates_session_binding() -> None:
    harness = LoginHarness()
    outcome = await harness.login()
    started = outcome.started_session
    assert started is not None
    failed = await harness.session_service.reauthenticate(
        session_secret=started.session_secret, password=_WRONG_PASSWORD
    )
    assert failed.public_error is ErrorCode.AUTHENTICATION_FAILED
    assert failed.rotated_session is None
    assert harness.sessions.rotation_commands == []

    succeeded = await harness.session_service.reauthenticate(
        session_secret=started.session_secret, password=_CORRECT_PASSWORD
    )
    assert succeeded.public_error is None
    rotated = succeeded.rotated_session
    assert rotated is not None
    assert rotated.session_secret != started.session_secret
    assert rotated.csrf_secret != started.csrf_secret
    stored = harness.sessions.rows_by_secret_hash[rotated.session_secret_hash]
    assert stored.reauthenticated_at == harness.database_now
    with pytest.raises(AuthenticationError):
        await harness.session_service.authenticate(session_secret=started.session_secret)


@pytest.mark.asyncio
async def test_logout_revokes_the_session_row() -> None:
    harness = LoginHarness()
    outcome = await harness.login()
    started = outcome.started_session
    assert started is not None
    revoked_at = await harness.session_service.revoke(session_secret=started.session_secret)
    assert revoked_at == harness.database_now
    stored = harness.sessions.rows_by_secret_hash[started.session_secret_hash]
    assert stored.state is WebSessionState.REVOKED
    assert stored.revoked_at == harness.database_now
    assert stored.revocation_reason == "logout"
    assert stored.authenticated_at is None
    assert stored.reauthenticated_at is None
    assert harness.sessions.revocation_commands[0].revocation_reason == "logout"


# --- password change ------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_change_requires_recent_authentication() -> None:
    harness = LoginHarness()
    outcome = await harness.login()
    started = outcome.started_session
    assert started is not None
    harness.clock.database_now_value = _DATABASE_NOW + timedelta(minutes=5, seconds=1)
    stale = await harness.password_service.change_password(
        session_secret=started.session_secret,
        new_password="fresh-rotation-passphrase-value",
        diagnostic_context=_diagnostic_context(),
    )
    assert stale.public_error is ErrorCode.RECENT_AUTHENTICATION_REQUIRED
    assert stale.rotated_session is None
    assert harness.credentials.change_commands == []


@pytest.mark.asyncio
async def test_password_change_rotates_current_and_revokes_other_sessions() -> None:
    harness = LoginHarness()
    outcome = await harness.login()
    started = outcome.started_session
    assert started is not None
    result = await harness.password_service.change_password(
        session_secret=started.session_secret,
        new_password="fresh-rotation-passphrase-value",
        diagnostic_context=_diagnostic_context(),
    )
    assert result.public_error is None
    assert result.credential_revision == 2
    assert result.revoked_session_count == 0
    rotated = result.rotated_session
    assert rotated is not None
    assert rotated.session_secret != started.session_secret
    assert rotated.csrf_secret != started.csrf_secret
    command = harness.credentials.change_commands[0]
    assert command.new_password_hash == harness.hasher.hashed_passwords[-1]
    assert command.expected_credential_revision == 1
    with pytest.raises(AuthenticationError):
        await harness.session_service.authenticate(session_secret=started.session_secret)


@pytest.mark.asyncio
async def test_password_change_shares_one_clock_read_between_gate_and_writes() -> None:
    gate_moment = _DATABASE_NOW + timedelta(minutes=5) - timedelta(seconds=1)
    later_moment = _DATABASE_NOW + timedelta(minutes=5) + timedelta(seconds=1)
    clock = AdvancingClock((_DATABASE_NOW, gate_moment, later_moment))
    harness = LoginHarness(clock=clock)
    started = (await harness.login()).started_session
    assert started is not None
    # The two scripted reads straddle the exclusive re-auth boundary: a second
    # read inside the same invocation could slide persisted state past the
    # moment the gate checked. One invocation must take exactly one read and
    # drive the gate, the session resolution and every write with it.
    reads_before_change = clock.read_count
    result = await harness.password_service.change_password(
        session_secret=started.session_secret,
        new_password="fresh-rotation-passphrase-value",
        diagnostic_context=_diagnostic_context(),
    )
    assert result.public_error is None
    assert clock.read_count - reads_before_change == 1
    command = harness.credentials.change_commands[0]
    assert command.database_now == gate_moment
    rotated = result.rotated_session
    assert rotated is not None
    stored = harness.sessions.rows_by_secret_hash[rotated.session_secret_hash]
    assert stored.last_seen_at == gate_moment
    assert stored.idle_expires_at == gate_moment + timedelta(hours=12)


@pytest.mark.asyncio
async def test_password_change_rejects_policy_violations_before_hashing() -> None:
    harness = LoginHarness()
    outcome = await harness.login()
    started = outcome.started_session
    assert started is not None
    short = await harness.password_service.change_password(
        session_secret=started.session_secret,
        new_password="too-short",
        diagnostic_context=_diagnostic_context(),
    )
    assert short.public_error is ErrorCode.AUTHENTICATION_FAILED
    assert short.rotated_session is None
    assert harness.hasher.hashed_passwords == []
    assert harness.credentials.change_commands == []


# --- pure throttle transitions --------------------------------------------------


def test_throttle_threshold_locks_exactly_on_the_fifth_failure() -> None:
    policy = ThrottleWindowPolicy()
    state: ThrottleBucketState | None = None
    for failure_number in range(1, LOGIN_FAILURE_THRESHOLD + 1):
        transition = next_login_failure_transition(
            state, database_now=_DATABASE_NOW, policy=policy
        )
        assert transition.failed_attempt_count == failure_number
        if failure_number < LOGIN_FAILURE_THRESHOLD:
            assert transition.locked_until is None
            assert not transition.became_locked
        else:
            assert transition.locked_until == _DATABASE_NOW + timedelta(minutes=15)
            assert transition.became_locked
        assert transition.window_started_at == _DATABASE_NOW
        state = ThrottleBucketState(
            window_started_at=transition.window_started_at,
            failed_attempt_count=transition.failed_attempt_count,
            locked_until=transition.locked_until,
        )


def test_throttle_window_rollover_restarts_the_count() -> None:
    policy = ThrottleWindowPolicy()
    stale = ThrottleBucketState(
        window_started_at=_DATABASE_NOW - timedelta(minutes=15, seconds=1),
        failed_attempt_count=4,
        locked_until=None,
    )
    transition = next_login_failure_transition(stale, database_now=_DATABASE_NOW, policy=policy)
    assert transition.failed_attempt_count == 1
    assert transition.window_started_at == _DATABASE_NOW
    assert transition.locked_until is None


def test_expired_lock_restarts_the_window() -> None:
    policy = ThrottleWindowPolicy()
    previously_locked = ThrottleBucketState(
        window_started_at=_DATABASE_NOW - timedelta(minutes=20),
        failed_attempt_count=5,
        locked_until=_DATABASE_NOW - timedelta(minutes=5),
    )
    assert not is_throttle_bucket_locked(previously_locked, database_now=_DATABASE_NOW)
    transition = next_login_failure_transition(
        previously_locked, database_now=_DATABASE_NOW, policy=policy
    )
    assert transition.failed_attempt_count == 1
    assert transition.locked_until is None
    assert is_throttle_bucket_locked(
        ThrottleBucketState(
            window_started_at=_DATABASE_NOW,
            failed_attempt_count=5,
            locked_until=_DATABASE_NOW + timedelta(minutes=1),
        ),
        database_now=_DATABASE_NOW,
    )


def test_successful_authentication_reset_clears_the_streak() -> None:
    reset = successful_authentication_reset(database_now=_DATABASE_NOW)
    assert reset.failed_attempt_count == 0
    assert reset.locked_until is None
    assert reset.window_started_at == _DATABASE_NOW


# --- pure session decisions ------------------------------------------------------


def test_session_decision_rejects_every_non_active_state() -> None:
    for state in (
        WebSessionState.PENDING_TOTP,
        WebSessionState.RECOVERY_LIMITED,
        WebSessionState.REVOKED,
    ):
        session = _active_session(state=state)
        decision = evaluate_session_authentication(
            session,
            current_credential_revision=1,
            database_now=_DATABASE_NOW,
            policy=SessionWindowPolicy(),
        )
        assert not decision.is_authenticated
        assert decision.rejection_code is ErrorCode.AUTHENTICATION_REQUIRED
        assert not decision.should_advance_activity


def test_session_decision_rejects_stale_revision_and_expiry() -> None:
    policy = SessionWindowPolicy()
    stale = evaluate_session_authentication(
        _active_session(),
        current_credential_revision=2,
        database_now=_DATABASE_NOW,
        policy=policy,
    )
    assert not stale.is_authenticated
    idle_expired = evaluate_session_authentication(
        _active_session(idle_expires_at=_DATABASE_NOW),
        current_credential_revision=1,
        database_now=_DATABASE_NOW,
        policy=policy,
    )
    assert not idle_expired.is_authenticated
    absolute_expired = evaluate_session_authentication(
        _active_session(absolute_expires_at=_DATABASE_NOW),
        current_credential_revision=1,
        database_now=_DATABASE_NOW,
        policy=policy,
    )
    assert not absolute_expired.is_authenticated


def test_recent_authentication_window_boundary_is_exclusive() -> None:
    session = _active_session()
    policy = SessionWindowPolicy()
    assert recent_authentication_moment(session) == _DATABASE_NOW
    assert is_recently_authenticated(
        session,
        database_now=_DATABASE_NOW + timedelta(minutes=5) - timedelta(seconds=1),
        policy=policy,
    )
    assert not is_recently_authenticated(
        session, database_now=_DATABASE_NOW + timedelta(minutes=5), policy=policy
    )
    reauthenticated = replace(session, reauthenticated_at=_DATABASE_NOW + timedelta(minutes=2))
    assert recent_authentication_moment(reauthenticated) == _DATABASE_NOW + timedelta(minutes=2)
    never_authenticated = replace(session, authenticated_at=None, reauthenticated_at=None)
    assert recent_authentication_moment(never_authenticated) is None
    assert not is_recently_authenticated(
        never_authenticated, database_now=_DATABASE_NOW, policy=policy
    )
