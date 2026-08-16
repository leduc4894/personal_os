"""Browser device-authorization grant state machine and services (spec 11).

This module owns the closed grant vocabulary of design section 11 as pure
functions, typed commands and results, and one service
(:class:`DeviceAuthorizationService`) that depends only on the authentication
ports: the grant transaction port implemented by the PostgreSQL adapter, the
session service the approval gate resolves through, the crypto port and the
transaction clock. Every persisted timestamp and expiry comparison uses the
single ``database_now`` of one service invocation; user-code and polling-secret
generation and their HMAC digests always happen outside the database
transactions, which commit once.

The user code is a short-lived human-readable ``CCCC-CCCC`` value over an
unambiguous 31-character alphabet whose eighth character is a weighted
checksum, so single-character typos fail closed before any lookup. The
polling credential is its own opaque versioned value — ``pg1.<lookup_id>.
<secret>`` with a 256-bit secret — distinct from the ``at1.``/``rt1.`` device
tokens of spec 12.1, because the spec pins those prefixes for access and
refresh tokens only; the chosen ``pg1.`` prefix keeps the polling credential
parseable by the same three-segment grammar. PostgreSQL stores only HMAC
digests of both values under the ``auth/grant-replay/v1`` subkey (spec 12.2,
20.1), so a leaked row cannot brute-force a live code or credential offline.
The complete verification URL carries the user code in the fragment and
nothing else — never the polling secret and never a query (spec 11.2).

Expiry is decided against ``expires_at`` while the grant is still pending; it
is not a stored state. Approve and deny are terminal transitions guarded by
row locks and the pending-state guard; approval additionally requires the
presented Web session to be active, to carry the
``device_authorization_approve`` scope and to have authenticated within the
recent re-authentication window (spec 9.4, 11.3). Denial is explicit and
terminal and needs the active session but not the recent window. The module
imports no infrastructure SDK, composition root or web framework.
"""

from __future__ import annotations

import base64
import hmac
import math
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable
from uuid import UUID, uuid7

from personal_os.authentication.contracts import (
    FIXED_DEVICE_SCOPE,
    DeviceAuthorizationGrantState,
    DeviceScope,
    WebScope,
)
from personal_os.authentication.crypto import GRANT_REPLAY_DERIVATION_LABEL
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.ports import AuthenticationCryptoPort
from personal_os.authentication.sessions import (
    AuthenticatedSession,
    AuthenticationClockPort,
    SessionService,
    SessionWindowPolicy,
    ThrottleBucketKind,
    ThrottleBucketState,
    ThrottleFailureTransition,
    derive_throttle_hmac_key,
    is_recently_authenticated,
    throttle_bucket_hash,
)
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode

#: Grant lifetime: exactly 600 seconds (spec 11.1, Global Constraints).
DEVICE_GRANT_EXPIRES_IN_SECONDS: Final[int] = 600
DEVICE_GRANT_LIFETIME: Final[timedelta] = timedelta(seconds=DEVICE_GRANT_EXPIRES_IN_SECONDS)

#: The minimum polling interval the plugin must respect (spec 11.4).
POLL_INTERVAL_SECONDS: Final[int] = 5

#: Device-name display bounds (spec 11.1: 1-80 display characters).
DEVICE_NAME_MINIMUM_LENGTH_CHARACTERS: Final[int] = 1
DEVICE_NAME_MAXIMUM_LENGTH_CHARACTERS: Final[int] = 80

#: Polling-secret entropy: exactly 32 random bytes, at least 256 bits (11.1).
POLLING_SECRET_ENTROPY_BYTES: Final[int] = 32

#: Live pending grants one client instance may hold at once (spec 11.1 cap).
MAXIMUM_LIVE_GRANTS_PER_CLIENT_INSTANCE: Final[int] = 5

#: Versioned polling-credential prefix (spec 12.1 leaves it unnamed; this is
#: the grant-polling member of the same opaque three-segment grammar).
POLLING_CREDENTIAL_PREFIX: Final[str] = "pg1"

#: The browser page that resolves a grant (spec 11.2).
VERIFICATION_PAGE_PATH: Final[str] = "/device/approve"

#: Unambiguous uppercase user-code alphabet: no I, O, letter L, 0 or 1, so
#: every character survives any font, handwriting or dictation (31 values).
USER_CODE_ALPHABET: Final[str] = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

#: The user-code grammar as one pattern string: two four-character blocks of
#: the unambiguous alphabet separated by exactly one hyphen.
USER_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"[{USER_CODE_ALPHABET}]{{4}}-[{USER_CODE_ALPHABET}]{{4}}"
)


#: Closed plugin platform classes (spec 11.1).
class DevicePlatformClass(StrEnum):
    """The closed plugin platform classes (spec 11.1)."""

    OBSIDIAN_DESKTOP = "obsidian_desktop"
    OBSIDIAN_MOBILE = "obsidian_mobile"


#: Closed supported platform token grammar (spec 11.1): lowercase dotted or
#: hyphenated platform tokens such as ``windows``, ``macos-14`` or
#: ``linux-x64`` bounded by the schema's 64 characters.
PLATFORM_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*")
PLATFORM_NAME_MAXIMUM_LENGTH_CHARACTERS: Final[int] = 64

#: Semantic plugin version grammar: dotted numeric triples (spec 11.1).
PLUGIN_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}")


@dataclass(frozen=True, slots=True)
class PluginVersionBounds:
    """The approved plugin version window of one runtime (spec 11.1, 23)."""

    minimum: tuple[int, int, int]
    maximum: tuple[int, int, int]

    @classmethod
    def from_strings(
        cls, *, minimum_plugin_version: str, maximum_plugin_version: str
    ) -> PluginVersionBounds:
        """Build the bounds from the two configured dotted triples."""
        minimum = parse_plugin_version(minimum_plugin_version)
        maximum = parse_plugin_version(maximum_plugin_version)
        if minimum > maximum:
            raise ValueError("minimum plugin version must not exceed the maximum")
        return cls(minimum=minimum, maximum=maximum)

    def safe_bounds(self) -> tuple[SafeToken, SafeToken]:
        """The approved bounds as the registered safe detail value."""
        return (
            SafeToken.parse(".".join(str(part) for part in self.minimum)),
            SafeToken.parse(".".join(str(part) for part in self.maximum)),
        )


def parse_plugin_version(plugin_version: str) -> tuple[int, int, int]:
    """Parse one semantic dotted numeric triple or reject it closed."""
    if PLUGIN_VERSION_PATTERN.fullmatch(plugin_version) is None:
        raise AuthenticationError(ErrorCode.PLUGIN_VERSION_UNSUPPORTED)
    parts = tuple(int(part) for part in plugin_version.split("."))
    return parts[0], parts[1], parts[2]


def validate_plugin_version_bounds(
    plugin_version: str, bounds: PluginVersionBounds
) -> tuple[int, int, int]:
    """Require one plugin version inside the approved window (spec 11.1).

    The rejection carries only the registered safe detail: the approved
    version bounds themselves, never the rejected value.
    """
    version = parse_plugin_version(plugin_version)
    if not bounds.minimum <= version <= bounds.maximum:
        raise AuthenticationError(
            ErrorCode.PLUGIN_VERSION_UNSUPPORTED,
            safe_details={"approved_version_bounds": bounds.safe_bounds()},
        )
    return version


# --- user code ------------------------------------------------------------------------


def _user_code_checksum(payload: str) -> str:
    """The eighth checksum character of one seven-character payload."""
    weighted_sum = sum(
        (position + 1) * USER_CODE_ALPHABET.index(character)
        for position, character in enumerate(payload)
    )
    return USER_CODE_ALPHABET[weighted_sum % len(USER_CODE_ALPHABET)]


def generate_user_code(*, random_bytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    """Generate one checksum-validated ``CCCC-CCCC`` user code (spec 11.1)."""
    payload = "".join(
        USER_CODE_ALPHABET[byte % len(USER_CODE_ALPHABET)] for byte in random_bytes(7)
    )
    characters = payload + _user_code_checksum(payload)
    return f"{characters[:4]}-{characters[4:]}"


def is_valid_user_code(value: str) -> bool:
    """Validate the closed grammar and checksum of one presented user code."""
    if not isinstance(value, str) or USER_CODE_PATTERN.fullmatch(value) is None:
        return False
    characters = value.replace("-", "")
    return _user_code_checksum(characters[:7]) == characters[7]


# --- polling credential ---------------------------------------------------------------


def generate_polling_credential(
    *, grant_id: UUID, random_bytes: Callable[[int], bytes] = secrets.token_bytes
) -> str:
    """Generate one ``pg1.<grant_id>.<secret>`` polling credential (spec 11.1).

    The secret segment is 43 unpadded base64url characters encoding exactly 32
    random bytes — at least the required 256 bits of entropy.
    """
    secret = base64.urlsafe_b64encode(random_bytes(POLLING_SECRET_ENTROPY_BYTES))
    return f"{POLLING_CREDENTIAL_PREFIX}.{grant_id}.{secret.rstrip(b'=').decode('ascii')}"


# --- browser continuity ---------------------------------------------------------------


def build_verification_uris(*, verification_base_url: str, user_code: str) -> tuple[str, str]:
    """Build the exact verification URLs of one grant (spec 11.2).

    The complete URL places the user code in the fragment and nothing else:
    no query, no polling secret, no token. The fragment never reaches a
    server log or the Origin header.
    """
    verification_uri = f"{verification_base_url.rstrip('/')}{VERIFICATION_PAGE_PATH}"
    return verification_uri, f"{verification_uri}#{user_code}"


# --- storage digests ------------------------------------------------------------------


def derive_grant_replay_hmac_key(crypto: AuthenticationCryptoPort, master_key: bytes) -> bytes:
    """Derive the ``auth/grant-replay/v1`` HMAC subkey (spec 12.2, 20.1)."""
    return crypto.derive_subkey(master_key=master_key, label=GRANT_REPLAY_DERIVATION_LABEL)


def user_code_hash_of(*, hmac_key: bytes, user_code: str) -> str:
    """The HMAC-SHA-256 digest stored for one user code."""
    return hmac.new(hmac_key, user_code.encode("utf-8"), digestmod="sha256").hexdigest()


def polling_credential_hash_of(*, hmac_key: bytes, polling_credential: str) -> str:
    """The HMAC-SHA-256 digest stored for one full polling credential.

    The digest covers the whole presented opaque value — prefix, lookup id
    and secret — so the Task 9 polling route verifies exactly what the plugin
    presents in its dedicated Bearer header.
    """
    return hmac.new(hmac_key, polling_credential.encode("ascii"), digestmod="sha256").hexdigest()


# --- typed row view and decisions -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredDeviceAuthorizationGrant:
    """Typed view of one ``device_authorization_grants`` row.

    The two secret digests stay with the adapter: the domain consumes grant
    material by identity and state only.
    """

    grant_id: UUID
    client_instance_id: UUID
    claimed_device_id: UUID | None
    device_name: str
    platform_class: DevicePlatformClass
    platform_name: str
    plugin_version: str
    requested_scope: DeviceScope
    state: DeviceAuthorizationGrantState
    created_at: datetime
    expires_at: datetime
    approved_at: datetime | None
    denied_at: datetime | None
    exchanged_at: datetime | None
    approved_by_user_id: UUID | None
    approved_web_session_id: UUID | None


def resolve_lookup_rejection_code(
    grant: StoredDeviceAuthorizationGrant | None, *, database_now: datetime
) -> ErrorCode | None:
    """Decide whether one looked-up grant may still be displayed (spec 11.3).

    Only a fresh pending grant resolves. An unknown code fails closed as an
    invalid device credential, a denied grant reports the denial, any other
    terminal state reports the closed state conflict, and a pending grant past
    ``expires_at`` reports the closed expiry.
    """
    if grant is None:
        return ErrorCode.DEVICE_CREDENTIAL_INVALID
    if grant.state is DeviceAuthorizationGrantState.DENIED:
        return ErrorCode.DEVICE_AUTHORIZATION_DENIED
    if grant.state is not DeviceAuthorizationGrantState.PENDING:
        return ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID
    if database_now >= grant.expires_at:
        return ErrorCode.DEVICE_AUTHORIZATION_EXPIRED
    return None


def resolve_terminal_rejection_code(
    grant: StoredDeviceAuthorizationGrant | None, *, database_now: datetime
) -> ErrorCode | None:
    """Decide whether one approve/deny transition may start (spec 11.3).

    The transition requires a pending, unexpired grant. An unknown grant id
    fails closed as an invalid device credential; expiry wins over the state
    conflict so the plugin stops polling at the closed 410.
    """
    if grant is None:
        return ErrorCode.DEVICE_CREDENTIAL_INVALID
    if grant.state is not DeviceAuthorizationGrantState.PENDING:
        return ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID
    if database_now >= grant.expires_at:
        return ErrorCode.DEVICE_AUTHORIZATION_EXPIRED
    return None


# --- transaction port -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiveGrantWindow:
    """The live pending grants of one client instance (spec 11.1 cap)."""

    live_grant_count: int
    earliest_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class InsertPendingGrantCommand:
    """One grant creation's transactional write."""

    grant_id: UUID
    user_code_hash: str = field(repr=False)
    polling_secret_hash: str = field(repr=False)
    client_instance_id: UUID
    claimed_device_id: UUID | None
    device_name: str
    platform_class: DevicePlatformClass
    platform_name: str
    plugin_version: str
    requested_scope: DeviceScope
    expires_at: datetime
    database_now: datetime
    creation_bucket_hash: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class InsertedPendingGrant:
    """The committed identity of one created grant."""

    grant_id: UUID
    expires_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class ApproveGrantCommand:
    """One approval's transactional write (spec 11.3)."""

    grant_id: UUID
    user_id: UUID
    workspace_id: UUID
    web_session_id: UUID
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class ApprovedGrant:
    """The committed approval."""

    grant_id: UUID
    state: DeviceAuthorizationGrantState
    approved_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class DenyGrantCommand:
    """One denial's transactional write (spec 11.3)."""

    grant_id: UUID
    user_id: UUID
    workspace_id: UUID
    web_session_id: UUID
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class DeniedGrant:
    """The committed denial."""

    grant_id: UUID
    state: DeviceAuthorizationGrantState
    denied_at: datetime
    database_now: datetime


@runtime_checkable
class DeviceAuthorizationTransactionPort(Protocol):
    """The grant transaction surface the service orchestrates."""

    async def resolve_throttle_bucket(
        self, *, bucket_kind: ThrottleBucketKind, bucket_hash: str
    ) -> ThrottleBucketState | None: ...

    async def record_throttle_attempt(
        self, *, bucket_kind: ThrottleBucketKind, bucket_hash: str, database_now: datetime
    ) -> ThrottleFailureTransition: ...

    async def live_grant_window(
        self, *, client_instance_id: UUID, database_now: datetime
    ) -> LiveGrantWindow: ...

    async def insert_pending_grant(
        self, command: InsertPendingGrantCommand
    ) -> InsertedPendingGrant: ...

    async def lookup_grant_by_user_code(
        self,
        *,
        user_code_hash: str,
        database_now: datetime,
        reset_bucket_hash: str | None = None,
    ) -> StoredDeviceAuthorizationGrant | None: ...

    async def approve_grant(self, command: ApproveGrantCommand) -> ApprovedGrant: ...

    async def deny_grant(self, command: DenyGrantCommand) -> DeniedGrant: ...


# --- service results ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreatedDeviceGrant:
    """One created grant: the one-time provisioning material of spec 11.1.

    The user code renders in the plugin and the approval page; the polling
    secret renders exactly once here and then lives only in SecretStorage.
    Both keep ``repr`` suppressed so no diagnostic sink can echo them.
    """

    grant_id: UUID
    user_code: str = field(repr=False)
    polling_secret: str = field(repr=False)
    verification_uri: str
    verification_uri_complete: str
    expires_in_seconds: int
    poll_interval_seconds: int
    expires_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class ResolvedDeviceGrant:
    """The approval-page display context of one pending grant (spec 11.3)."""

    grant_id: UUID
    user_code: str = field(repr=False)
    device_name: str
    platform_class: DevicePlatformClass
    platform_name: str
    plugin_version: str
    requested_scope: DeviceScope
    expires_at: datetime
    database_now: datetime


def _retry_after_seconds(locked_until: datetime, *, database_now: datetime) -> int:
    """The registered safe retry hint of one rate-limited exit."""
    return max(1, math.ceil((locked_until - database_now).total_seconds()))


def _rate_limited(locked_until: datetime, *, database_now: datetime) -> AuthenticationError:
    retry_after_seconds = _retry_after_seconds(locked_until, database_now=database_now)
    return AuthenticationError(
        ErrorCode.AUTHENTICATION_RATE_LIMITED,
        safe_details={"retry_after_seconds": retry_after_seconds},
    )


def _active_lock_of(locked_until: datetime | None, *, database_now: datetime) -> datetime | None:
    if locked_until is None:
        return None
    return locked_until if database_now < locked_until else None


# --- service --------------------------------------------------------------------------


class DeviceAuthorizationService:
    """The grant creation, lookup, approval and denial choreography (spec 11).

    One invocation of any method takes exactly one ``database_now`` read,
    generates and hashes secret material outside every transaction, and
    commits through the transaction port's single-purpose transactions.
    Creation enforces the approved plugin version window before any secret
    exists, throttles per source through the ``grant_creation`` bucket and
    caps live grants per client instance; lookup throttles per authenticated
    user through the ``user_code_lookup`` bucket and resolves only fresh
    pending grants; approval resolves the presented session, enforces the
    ``device_authorization_approve`` scope and the recent re-authentication
    window before the locked transition, while denial requires the active
    session without the recent window (spec 9.4, 11.3).
    """

    def __init__(
        self,
        *,
        grants: DeviceAuthorizationTransactionPort,
        session_service: SessionService,
        crypto: AuthenticationCryptoPort,
        master_key: bytes,
        clock: AuthenticationClockPort,
        plugin_version_bounds: PluginVersionBounds,
        verification_base_url: str,
        session_policy: SessionWindowPolicy | None = None,
    ) -> None:
        self._grants = grants
        self._session_service = session_service
        self._clock = clock
        self.plugin_version_bounds = plugin_version_bounds
        self.verification_base_url = verification_base_url
        self.session_policy = (
            session_policy if session_policy is not None else SessionWindowPolicy()
        )
        self._grant_hmac_key = derive_grant_replay_hmac_key(crypto, master_key)
        self._throttle_hmac_key = derive_throttle_hmac_key(crypto, master_key)

    async def database_now(self) -> datetime:
        """One transaction timestamp shared with co-orchestrating services."""
        return await self._clock.database_now()

    # -- creation (spec 11.1) ----------------------------------------------------------

    async def create_grant(
        self,
        *,
        client_instance_id: UUID,
        device_name: str,
        platform_class: DevicePlatformClass,
        platform_name: str,
        plugin_version: str,
        requested_scope: DeviceScope,
        source_bucket: str,
        claimed_device_id: UUID | None = None,
        diagnostic_context: DiagnosticContext | None = None,
    ) -> CreatedDeviceGrant:
        """Run one plugin grant creation and return the provisioning payload.

        The request is fully validated — plugin version against the approved
        window, platform class and scope against their closed vocabularies and
        the device name against its display bounds — before any secret is
        generated. The creation attempt counts into the source's
        ``grant_creation`` throttle bucket inside the inserting transaction.
        """
        del diagnostic_context  # creation writes no audit row (spec 21)
        database_now = await self._clock.database_now()
        creation_bucket_hash = self._bucket_hash(ThrottleBucketKind.GRANT_CREATION, source_bucket)
        bucket = await self._grants.resolve_throttle_bucket(
            bucket_kind=ThrottleBucketKind.GRANT_CREATION,
            bucket_hash=creation_bucket_hash,
        )
        locked_until = _active_lock_of(
            bucket.locked_until if bucket is not None else None,
            database_now=database_now,
        )
        if locked_until is not None:
            raise _rate_limited(locked_until, database_now=database_now)
        try:
            validate_plugin_version_bounds(plugin_version, self.plugin_version_bounds)
            self._validate_request_values(
                device_name=device_name,
                platform_class=platform_class,
                platform_name=platform_name,
                requested_scope=requested_scope,
            )
        except AuthenticationError:
            # A rejected attempt still counts toward the source bound.
            await self._grants.record_throttle_attempt(
                bucket_kind=ThrottleBucketKind.GRANT_CREATION,
                bucket_hash=creation_bucket_hash,
                database_now=database_now,
            )
            raise
        window = await self._grants.live_grant_window(
            client_instance_id=client_instance_id, database_now=database_now
        )
        if window.live_grant_count >= MAXIMUM_LIVE_GRANTS_PER_CLIENT_INSTANCE:
            retry_until = window.earliest_expires_at
            if retry_until is None or retry_until <= database_now:
                retry_until = database_now + DEVICE_GRANT_LIFETIME
            raise _rate_limited(retry_until, database_now=database_now)
        grant_id = uuid7()
        expires_at = database_now + DEVICE_GRANT_LIFETIME
        user_code = generate_user_code()
        polling_secret = generate_polling_credential(grant_id=grant_id)
        verification_uri, verification_uri_complete = build_verification_uris(
            verification_base_url=self.verification_base_url, user_code=user_code
        )
        await self._grants.insert_pending_grant(
            InsertPendingGrantCommand(
                grant_id=grant_id,
                user_code_hash=user_code_hash_of(
                    hmac_key=self._grant_hmac_key, user_code=user_code
                ),
                polling_secret_hash=polling_credential_hash_of(
                    hmac_key=self._grant_hmac_key, polling_credential=polling_secret
                ),
                client_instance_id=client_instance_id,
                claimed_device_id=claimed_device_id,
                device_name=device_name,
                platform_class=platform_class,
                platform_name=platform_name,
                plugin_version=plugin_version,
                requested_scope=requested_scope,
                expires_at=expires_at,
                database_now=database_now,
                creation_bucket_hash=creation_bucket_hash,
            )
        )
        return CreatedDeviceGrant(
            grant_id=grant_id,
            user_code=user_code,
            polling_secret=polling_secret,
            verification_uri=verification_uri,
            verification_uri_complete=verification_uri_complete,
            expires_in_seconds=DEVICE_GRANT_EXPIRES_IN_SECONDS,
            poll_interval_seconds=POLL_INTERVAL_SECONDS,
            expires_at=expires_at,
            database_now=database_now,
        )

    # -- browser lookup (spec 11.2/11.3) ------------------------------------------------

    async def lookup_grant(
        self,
        *,
        user_code: str,
        user_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> ResolvedDeviceGrant:
        """Resolve one user code to its approval-page display context.

        Only the grammar-and-checksum-valid code of a fresh pending grant
        resolves; every other outcome maps to its closed code (spec 17).
        Unknown or invalid codes count into the user's ``user_code_lookup``
        bucket and lock it at the shared bound; a resolvable lookup resets
        the streak inside the same transaction.
        """
        del diagnostic_context  # the lookup is a read with no audit row
        database_now = await self._clock.database_now()
        lookup_bucket_hash = self._bucket_hash(ThrottleBucketKind.USER_CODE_LOOKUP, str(user_id))
        bucket = await self._grants.resolve_throttle_bucket(
            bucket_kind=ThrottleBucketKind.USER_CODE_LOOKUP,
            bucket_hash=lookup_bucket_hash,
        )
        locked_until = _active_lock_of(
            bucket.locked_until if bucket is not None else None,
            database_now=database_now,
        )
        if locked_until is not None:
            raise _rate_limited(locked_until, database_now=database_now)
        if not is_valid_user_code(user_code):
            await self._grants.record_throttle_attempt(
                bucket_kind=ThrottleBucketKind.USER_CODE_LOOKUP,
                bucket_hash=lookup_bucket_hash,
                database_now=database_now,
            )
            raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
        stored = await self._grants.lookup_grant_by_user_code(
            user_code_hash=user_code_hash_of(hmac_key=self._grant_hmac_key, user_code=user_code),
            database_now=database_now,
            reset_bucket_hash=lookup_bucket_hash,
        )
        rejection_code = resolve_lookup_rejection_code(stored, database_now=database_now)
        if rejection_code is ErrorCode.DEVICE_CREDENTIAL_INVALID:
            await self._grants.record_throttle_attempt(
                bucket_kind=ThrottleBucketKind.USER_CODE_LOOKUP,
                bucket_hash=lookup_bucket_hash,
                database_now=database_now,
            )
        if rejection_code is not None:
            raise AuthenticationError(rejection_code)
        assert stored is not None
        return ResolvedDeviceGrant(
            grant_id=stored.grant_id,
            user_code=user_code,
            device_name=stored.device_name,
            platform_class=stored.platform_class,
            platform_name=stored.platform_name,
            plugin_version=stored.plugin_version,
            requested_scope=stored.requested_scope,
            expires_at=stored.expires_at,
            database_now=database_now,
        )

    # -- terminal decisions (spec 11.3) --------------------------------------------------

    async def approve_grant(
        self,
        *,
        grant_id: UUID,
        session_secret: str,
        diagnostic_context: DiagnosticContext,
    ) -> ApprovedGrant:
        """Approve one grant behind the recent-authentication gate (11.3)."""
        database_now = await self._clock.database_now()
        resolved = await self._session_service.resolve(
            session_secret=session_secret, database_now=database_now
        )
        self._require_decision_authority(resolved)
        if not is_recently_authenticated(
            resolved.session,
            database_now=database_now,
            policy=self._session_service.session_policy,
        ):
            raise AuthenticationError(ErrorCode.RECENT_AUTHENTICATION_REQUIRED)
        return await self._grants.approve_grant(
            ApproveGrantCommand(
                grant_id=grant_id,
                user_id=resolved.context.user_id,
                workspace_id=resolved.context.workspace_id,
                web_session_id=resolved.context.web_session_id,
                database_now=database_now,
                diagnostic_context=diagnostic_context,
            )
        )

    async def deny_grant(
        self,
        *,
        grant_id: UUID,
        session_secret: str,
        diagnostic_context: DiagnosticContext,
    ) -> DeniedGrant:
        """Deny one grant: explicit, terminal, no recent window (spec 11.3)."""
        database_now = await self._clock.database_now()
        resolved = await self._session_service.resolve(
            session_secret=session_secret, database_now=database_now
        )
        self._require_decision_authority(resolved)
        return await self._grants.deny_grant(
            DenyGrantCommand(
                grant_id=grant_id,
                user_id=resolved.context.user_id,
                workspace_id=resolved.context.workspace_id,
                web_session_id=resolved.context.web_session_id,
                database_now=database_now,
                diagnostic_context=diagnostic_context,
            )
        )

    # -- internal helpers ----------------------------------------------------------------

    @staticmethod
    def _require_decision_authority(resolved: AuthenticatedSession) -> None:
        """Enforce the explicit approval scope of the active session (6.1)."""
        if WebScope.DEVICE_AUTHORIZATION_APPROVE not in resolved.context.scopes:
            raise AuthenticationError(ErrorCode.AUTHORIZATION_SCOPE_DENIED)

    def _bucket_hash(self, bucket_kind: ThrottleBucketKind, material: str) -> str:
        return throttle_bucket_hash(
            hmac_key=self._throttle_hmac_key,
            bucket_kind=bucket_kind,
            bucket_material=material,
        )

    @staticmethod
    def _validate_request_values(
        *,
        device_name: str,
        platform_class: DevicePlatformClass,
        platform_name: str,
        requested_scope: DeviceScope,
    ) -> None:
        """Re-validate the closed request values before secret generation."""
        if (
            len(device_name) < DEVICE_NAME_MINIMUM_LENGTH_CHARACTERS
            or len(device_name) > DEVICE_NAME_MAXIMUM_LENGTH_CHARACTERS
            or platform_class not in DevicePlatformClass
            or PLATFORM_NAME_PATTERN.fullmatch(platform_name) is None
            or len(platform_name) > PLATFORM_NAME_MAXIMUM_LENGTH_CHARACTERS
            or requested_scope is not FIXED_DEVICE_SCOPE
        ):
            raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
