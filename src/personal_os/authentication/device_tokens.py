"""Rotating device credentials: derivations, replay classification, service.

This module owns the exact-replay credential machinery of design sections 12
and 13 as pure functions, typed row views, commands and one service
(:class:`DeviceTokenService`) that depends only on the grant/token
transaction ports, the versioned keyring view and the transaction clock.

Every credential secret is derived, never random, so a lost commit
acknowledgement is recoverable byte-for-byte: the refresh successor secret is
the HMAC-SHA-256 of the ``auth/refresh-replay/v1`` subkey over the
predecessor secret, the rotation identity, the family identity and the
big-endian successor generation (spec 13.4); the initial access and refresh
secrets derive under their own pinned labels over the presented polling
secret, the grant identity, the token lookup identity and the generation
(spec 12.2). PostgreSQL stores only HMAC digests. Derivations use pure
stdlib HKDF-SHA-256/HMAC constructions — RFC 5869 extract-and-expand with a
32-byte zero salt — so the domain stays free of any crypto implementation
package while the byte layout matches the composition root's adapter exactly.

The service takes exactly one ``database_now`` read per invocation, derives
and hashes every secret outside the database transactions, and commits
through single-purpose transactions: the exchange locks the grant by its
polling-secret digest, creates one device, family, access token and refresh
token, anchors their identities on the grant and appends the registration
audits in one commit; the rotation locks the predecessor and family,
classifies exact replay versus confirmed reuse versus a new rotation, rotates
the predecessor before inserting the successor and never extends the absolute
expiry; access authentication verifies the hash under the row's derivation
key and the token/family/device/user/workspace state on every request,
updating the device last-seen stamp at most once per five minutes. The
five-second pending poll pace lives in a bounded in-memory window: the
personal-OS API serves from one process, and a restart resets every plugin to
the documented starting interval (spec 11.4).

The module imports no infrastructure SDK, composition root or web framework.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable
from uuid import UUID, uuid7

from personal_os.authentication.contracts import (
    AuthenticatedDeviceContext,
    DeviceTokenFamilyState,
    DeviceTokenKind,
    DeviceTokenState,
    WebScope,
)
from personal_os.authentication.crypto import (
    ACCESS_CREDENTIAL_DERIVATION_LABEL,
    ACCESS_CREDENTIAL_PREFIX,
    EXCHANGE_CREDENTIAL_DERIVATION_LABEL,
    REFRESH_CREDENTIAL_PREFIX,
    REFRESH_REPLAY_DERIVATION_LABEL,
    parse_access_credential,
    parse_refresh_credential,
)
from personal_os.authentication.device_authorization import (
    POLL_INTERVAL_SECONDS,
    derive_grant_replay_hmac_key,
    polling_credential_hash_of,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.ports import AuthenticationCryptoPort
from personal_os.authentication.sessions import (
    AuthenticatedSession,
    AuthenticationClockPort,
    SessionService,
    is_recently_authenticated,
)
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError

#: Access tokens expire exactly 15 minutes after issue (spec 13.1).
ACCESS_TOKEN_LIFETIME_SECONDS: Final[int] = 15 * 60
ACCESS_TOKEN_LIFETIME: Final[timedelta] = timedelta(seconds=ACCESS_TOKEN_LIFETIME_SECONDS)

#: A refresh family lapses after 30 days without a successful rotation (13.2).
REFRESH_INACTIVITY_LIFETIME: Final[timedelta] = timedelta(days=30)
REFRESH_INACTIVITY_LIFETIME_SECONDS: Final[int] = int(REFRESH_INACTIVITY_LIFETIME.total_seconds())

#: A refresh family never outlives 90 days from creation (spec 13.2).
REFRESH_ABSOLUTE_LIFETIME: Final[timedelta] = timedelta(days=90)

#: Exchange creates the initial token pair at generation one (spec 15.7).
INITIAL_REFRESH_GENERATION: Final[int] = 1

#: ``devices.last_seen_at`` advances at most once per five minutes (13.1).
DEVICE_LAST_SEEN_MAXIMUM_UPDATE_INTERVAL: Final[timedelta] = timedelta(minutes=5)

#: Audit actions of the two revoke paths (spec 14, 21): the Admin device
#: revocation and the terminal family revoke the plugin self-revoke commits.
DEVICE_REVOKED_AUDIT_ACTION: Final[str] = "authentication.device_revoked"
DEVICE_TOKEN_FAMILY_REVOKED_AUDIT_ACTION: Final[str] = "authentication.device_token_family_revoked"

#: Closed family revocation reasons of the two revoke paths (spec 14).
ADMIN_REVOCATION_REASON: Final[str] = "admin_revoked"
SELF_REVOCATION_REASON: Final[str] = "self_revoked"

#: Polling credential grammar bounds (spec 11.1): url-safe secret segment.
POLLING_CREDENTIAL_PREFIX: Final[str] = "pg1"
POLLING_CREDENTIAL_MAXIMUM_LENGTH_CHARACTERS: Final[int] = 512
POLLING_CREDENTIAL_SECRET_MINIMUM_LENGTH_CHARACTERS: Final[int] = 16
POLLING_CREDENTIAL_SECRET_MAXIMUM_LENGTH_CHARACTERS: Final[int] = 128

#: HKDF-SHA-256 salt: 32 zero bytes, the RFC 5869 default salt length.
_HKDF_SALT_BYTES: Final[bytes] = bytes(32)

#: Derived secrets are exactly 32 bytes (256 bits).
_DERIVED_SECRET_SIZE_BYTES: Final[int] = 32

#: Poll-pace bounds: the starting interval doubles on every too-fast poll up
#: to the ceiling, and the window forgets the least recently polled grant
#: once it holds this many entries (spec 11.4).
_MAXIMUM_POLL_INTERVAL_SECONDS: Final[int] = 60
_POLL_PACE_WINDOW_MAXIMUM_GRANTS: Final[int] = 1024


# --- RFC 5869 HKDF-SHA-256 over the stdlib -------------------------------------------


def _hkdf_sha256(*, salt: bytes, input_key_material: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 extract-and-expand with HMAC-SHA-256, stdlib only."""
    pseudo_random_key = hmac.new(salt, input_key_material, hashlib.sha256).digest()
    output_key_material = b""
    block = b""
    counter = 1
    while len(output_key_material) < length:
        block = hmac.new(
            pseudo_random_key, block + info + bytes([counter]), hashlib.sha256
        ).digest()
        output_key_material += block
        counter += 1
    return output_key_material[:length]


def _derivation_subkey(*, master_key: bytes, label: str) -> bytes:
    """Derive one 32-byte domain subkey under the pinned HKDF construction."""
    return _hkdf_sha256(
        salt=_HKDF_SALT_BYTES,
        input_key_material=master_key,
        info=label.encode("ascii"),
        length=_DERIVED_SECRET_SIZE_BYTES,
    )


@dataclass(frozen=True, slots=True)
class DerivedTokenSecret:
    """One derived credential secret; the material never renders."""

    secret: bytes = field(repr=False)


# --- exact-replay derivations ---------------------------------------------------------


def _rotation_message(
    *,
    predecessor_secret: bytes,
    rotation_id: UUID,
    token_family_id: UUID,
    successor_generation: int,
) -> bytes:
    """The exact replay-stable message of one rotation derivation (13.4)."""
    return b"".join(
        (
            predecessor_secret,
            rotation_id.bytes,
            token_family_id.bytes,
            successor_generation.to_bytes(8, "big"),
        )
    )


def derive_refresh_successor(
    *,
    master_key: bytes,
    predecessor_secret: bytes,
    rotation_id: UUID,
    token_family_id: UUID,
    successor_generation: int,
) -> DerivedTokenSecret:
    """Derive one successor refresh secret (spec 13.4, golden vector).

    The PRF key is RFC 5869 HKDF-SHA-256 (32-byte zero salt) of the master
    key under ``auth/refresh-replay/v1``; the secret is HMAC-SHA-256 over
    ``predecessor_secret || rotation_id.bytes || token_family_id.bytes ||
    successor_generation`` in eight big-endian bytes.
    """
    pseudo_random_key = _derivation_subkey(
        master_key=master_key, label=REFRESH_REPLAY_DERIVATION_LABEL
    )
    return DerivedTokenSecret(
        secret=hmac.new(
            pseudo_random_key,
            _rotation_message(
                predecessor_secret=predecessor_secret,
                rotation_id=rotation_id,
                token_family_id=token_family_id,
                successor_generation=successor_generation,
            ),
            hashlib.sha256,
        ).digest()
    )


def derive_rotation_access_credential(
    *,
    master_key: bytes,
    predecessor_secret: bytes,
    rotation_id: UUID,
    token_family_id: UUID,
    successor_generation: int,
) -> DerivedTokenSecret:
    """Derive the 15-minute access secret minted with one rotation (13.4).

    Same message layout as :func:`derive_refresh_successor` under the
    ``auth/access-credential/v1`` subkey, so the access and refresh secrets
    of one rotation can never collide.
    """
    pseudo_random_key = _derivation_subkey(
        master_key=master_key, label=ACCESS_CREDENTIAL_DERIVATION_LABEL
    )
    return DerivedTokenSecret(
        secret=hmac.new(
            pseudo_random_key,
            _rotation_message(
                predecessor_secret=predecessor_secret,
                rotation_id=rotation_id,
                token_family_id=token_family_id,
                successor_generation=successor_generation,
            ),
            hashlib.sha256,
        ).digest()
    )


def _exchange_message(
    *,
    polling_secret: bytes,
    grant_id: UUID,
    token_lookup_id: UUID,
    generation: int,
) -> bytes:
    """The exact replay-stable message of one exchange derivation (12.2)."""
    return b"".join(
        (
            polling_secret,
            grant_id.bytes,
            token_lookup_id.bytes,
            generation.to_bytes(8, "big"),
        )
    )


def derive_exchange_refresh_credential(
    *,
    master_key: bytes,
    polling_secret: bytes,
    grant_id: UUID,
    token_lookup_id: UUID,
    generation: int = INITIAL_REFRESH_GENERATION,
) -> DerivedTokenSecret:
    """Derive the initial refresh secret of one grant exchange (spec 12.2).

    The PRF runs under ``auth/exchange-credential/v1`` over the presented
    polling secret, the grant identity, the token lookup identity and the
    generation, so the same polling secret re-derives the byte-identical
    credential while the initial generation stays current.
    """
    pseudo_random_key = _derivation_subkey(
        master_key=master_key, label=EXCHANGE_CREDENTIAL_DERIVATION_LABEL
    )
    return DerivedTokenSecret(
        secret=hmac.new(
            pseudo_random_key,
            _exchange_message(
                polling_secret=polling_secret,
                grant_id=grant_id,
                token_lookup_id=token_lookup_id,
                generation=generation,
            ),
            hashlib.sha256,
        ).digest()
    )


def derive_exchange_access_credential(
    *,
    master_key: bytes,
    polling_secret: bytes,
    grant_id: UUID,
    token_lookup_id: UUID,
    generation: int = INITIAL_REFRESH_GENERATION,
) -> DerivedTokenSecret:
    """Derive the initial access secret of one grant exchange (spec 12.2).

    Same exchange inputs as the refresh derivation under the
    ``auth/access-credential/v1`` subkey with the access lookup identity.
    """
    pseudo_random_key = _derivation_subkey(
        master_key=master_key, label=ACCESS_CREDENTIAL_DERIVATION_LABEL
    )
    return DerivedTokenSecret(
        secret=hmac.new(
            pseudo_random_key,
            _exchange_message(
                polling_secret=polling_secret,
                grant_id=grant_id,
                token_lookup_id=token_lookup_id,
                generation=generation,
            ),
            hashlib.sha256,
        ).digest()
    )


def refresh_secret_hash_of(*, master_key: bytes, secret: bytes) -> str:
    """The stored digest of one refresh secret under the refresh subkey."""
    pseudo_random_key = _derivation_subkey(
        master_key=master_key, label=REFRESH_REPLAY_DERIVATION_LABEL
    )
    return hmac.new(pseudo_random_key, secret, hashlib.sha256).hexdigest()


def access_secret_hash_of(*, master_key: bytes, secret: bytes) -> str:
    """The stored digest of one access secret under the access subkey."""
    pseudo_random_key = _derivation_subkey(
        master_key=master_key, label=ACCESS_CREDENTIAL_DERIVATION_LABEL
    )
    return hmac.new(pseudo_random_key, secret, hashlib.sha256).hexdigest()


# --- credential formatting and parsing ------------------------------------------------


def _format_credential(*, prefix: str, lookup_id: UUID, secret: bytes) -> str:
    return f"{prefix}.{lookup_id}.{secret.hex()}"


def format_access_credential(*, lookup_id: UUID, secret: bytes) -> str:
    """Render one ``at1.<lookup_id>.<secret>`` access credential (12.1)."""
    return _format_credential(prefix=ACCESS_CREDENTIAL_PREFIX, lookup_id=lookup_id, secret=secret)


def format_refresh_credential(*, lookup_id: UUID, secret: bytes) -> str:
    """Render one ``rt1.<lookup_id>.<secret>`` refresh credential (12.1)."""
    return _format_credential(prefix=REFRESH_CREDENTIAL_PREFIX, lookup_id=lookup_id, secret=secret)


@dataclass(frozen=True, slots=True)
class ParsedPollingCredential:
    """One parsed ``pg1.<grant_id>.<secret>`` polling credential (11.4)."""

    grant_id: UUID
    secret: bytes = field(repr=False)


def parse_polling_credential(value: str) -> ParsedPollingCredential:
    """Parse one polling credential or fail closed without echoing it.

    The value follows the same three-segment grammar as the device
    credentials with the ``pg1`` version prefix; anything else is the closed
    ``device_credential_invalid`` rejection carrying no trace of the input.
    """
    if (
        not isinstance(value, str)
        or not value
        or len(value) > (POLLING_CREDENTIAL_MAXIMUM_LENGTH_CHARACTERS)
    ):
        raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
    segments = value.split(".")
    if len(segments) != 3:
        raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
    version_segment, lookup_id_segment, secret_segment = segments
    if version_segment != POLLING_CREDENTIAL_PREFIX:
        raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
    try:
        grant_id = UUID(lookup_id_segment)
    except ValueError as cause:
        raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID) from cause
    if not (
        POLLING_CREDENTIAL_SECRET_MINIMUM_LENGTH_CHARACTERS
        <= len(secret_segment)
        <= POLLING_CREDENTIAL_SECRET_MAXIMUM_LENGTH_CHARACTERS
        and secret_segment.isascii()
        and all(character.isalnum() or character in "-_" for character in secret_segment)
    ):
        raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
    return ParsedPollingCredential(grant_id=grant_id, secret=secret_segment.encode("ascii"))


# --- typed row views ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredDeviceToken:
    """Typed view of one ``device_tokens`` row (spec 15.7).

    The stored secret digest stays with the adapter; the domain consumes
    lineage and replay state only.
    """

    device_token_id: UUID
    token_family_id: UUID
    user_id: UUID
    workspace_id: UUID
    device_id: UUID
    token_kind: DeviceTokenKind = DeviceTokenKind.REFRESH
    generation: int = 1
    state: DeviceTokenState = DeviceTokenState.ACTIVE
    predecessor_token_id: UUID | None = None
    successor_token_id: UUID | None = None
    rotation_id: UUID | None = None
    derivation_key_id: str = ""
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    rotated_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StoredTokenFamily:
    """Typed view of one ``device_token_families`` row (spec 15.6)."""

    token_family_id: UUID
    user_id: UUID
    workspace_id: UUID
    device_id: UUID
    state: DeviceTokenFamilyState = DeviceTokenFamilyState.ACTIVE
    current_refresh_generation: int = 1
    created_at: datetime | None = None
    last_refreshed_at: datetime | None = None
    inactivity_expires_at: datetime | None = None
    absolute_expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revocation_reason: str | None = None


# --- replay classification (spec 13.4, 13.5) ------------------------------------------


class RefreshPresentationKind(StrEnum):
    """The closed outcomes of one refresh presentation."""

    NEW_ROTATION = "new_rotation"
    EXACT_REPLAY = "exact_replay"
    REUSE_DETECTED = "reuse_detected"


def classify_refresh_presentation(
    *,
    predecessor: StoredDeviceToken,
    successor: StoredDeviceToken | None,
    family: StoredTokenFamily,
    presented_rotation_id: UUID,
    database_now: datetime,
) -> RefreshPresentationKind:
    """Classify one locked refresh presentation (spec 13.4, 13.5).

    A rotated predecessor replays exactly when the presented rotation
    identity matches its committed successor and that successor is still the
    family's current active generation of an unrevoked family. Everything
    else a rotated state can mean — a different rotation identity, a
    successor that has rotated again, broken lineage — plus a revoked
    credential used as current, an active predecessor that is no longer the
    current generation, or a family past either expiry window, is confirmed
    reuse. Only an active, current, unexpired predecessor of an active
    family inside both windows with a new rotation identity is a new
    rotation.
    """
    if family.state is DeviceTokenFamilyState.REVOKED:
        return RefreshPresentationKind.REUSE_DETECTED
    if predecessor.state is DeviceTokenState.REVOKED:
        return RefreshPresentationKind.REUSE_DETECTED
    if predecessor.state is DeviceTokenState.ROTATED:
        if successor is None or successor.rotation_id is None:
            return RefreshPresentationKind.REUSE_DETECTED
        is_committed_rotation = successor.rotation_id == presented_rotation_id
        is_current_generation = (
            successor.state is DeviceTokenState.ACTIVE
            and family.current_refresh_generation == successor.generation
        )
        if is_committed_rotation and is_current_generation:
            return RefreshPresentationKind.EXACT_REPLAY
        return RefreshPresentationKind.REUSE_DETECTED
    # Active predecessor: it must still be the current generation and live.
    if family.current_refresh_generation != predecessor.generation:
        return RefreshPresentationKind.REUSE_DETECTED
    if predecessor.expires_at is None or database_now >= predecessor.expires_at:
        return RefreshPresentationKind.REUSE_DETECTED
    if family.inactivity_expires_at is not None and database_now >= family.inactivity_expires_at:
        return RefreshPresentationKind.REUSE_DETECTED
    if family.absolute_expires_at is not None and database_now >= family.absolute_expires_at:
        return RefreshPresentationKind.REUSE_DETECTED
    return RefreshPresentationKind.NEW_ROTATION


# --- poll pacing (spec 11.4) -----------------------------------------------------------


class GrantPollPacer:
    """Bounded in-memory pending-poll pace window (spec 11.4).

    The pacer counts only pending polls — an approved poll exchanges and a
    replayed poll re-derives committed material, neither of which waits. An
    accepted poll registers its timestamp; a too-fast poll doubles the
    grant's minimum interval up to the ceiling without moving the timestamp,
    so the plugin's next allowed poll stays one minimum interval after the
    last accepted one. The window forgets the least recently polled grant
    past its bound; a process restart resets every plugin to the documented
    five-second starting interval.
    """

    def __init__(
        self,
        *,
        starting_interval_seconds: int = POLL_INTERVAL_SECONDS,
        maximum_interval_seconds: int = _MAXIMUM_POLL_INTERVAL_SECONDS,
        maximum_tracked_grants: int = _POLL_PACE_WINDOW_MAXIMUM_GRANTS,
    ) -> None:
        self._starting_interval_seconds = starting_interval_seconds
        self._maximum_interval_seconds = maximum_interval_seconds
        self._maximum_tracked_grants = maximum_tracked_grants
        self._windows: OrderedDict[UUID, tuple[datetime, int]] = OrderedDict()

    def register_pending_poll(self, grant_id: UUID, database_now: datetime) -> int | None:
        """Register one pending poll; return the closed slow-down hint.

        ``None`` means the poll paces within its current minimum interval; a
        positive value is the whole seconds remaining until the next allowed
        poll.
        """
        window = self._windows.get(grant_id)
        if window is None:
            self._remember(grant_id, (database_now, self._starting_interval_seconds))
            return None
        last_polled_at, minimum_interval_seconds = window
        elapsed = database_now - last_polled_at
        if elapsed < timedelta(seconds=minimum_interval_seconds):
            doubled = min(minimum_interval_seconds * 2, self._maximum_interval_seconds)
            self._windows[grant_id] = (last_polled_at, doubled)
            return max(
                1,
                math.ceil((timedelta(seconds=minimum_interval_seconds) - elapsed).total_seconds()),
            )
        self._remember(grant_id, (database_now, minimum_interval_seconds))
        return None

    def _remember(self, grant_id: UUID, window: tuple[datetime, int]) -> None:
        self._windows[grant_id] = window
        self._windows.move_to_end(grant_id)
        while len(self._windows) > self._maximum_tracked_grants:
            self._windows.popitem(last=False)


# --- transaction ports -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExchangeGrantCommand:
    """One grant exchange's transactional writes (spec 12)."""

    grant_id: UUID
    polling_secret_hash: str = field(repr=False)
    device_id: UUID
    token_family_id: UUID
    access_token_id: UUID
    refresh_token_id: UUID
    access_secret_hash: str = field(repr=False)
    refresh_secret_hash: str = field(repr=False)
    derivation_key_id: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    family_absolute_expires_at: datetime
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class ExchangeProvisioning:
    """The committed — or replayed — identity of one grant exchange."""

    grant_id: UUID
    device_id: UUID
    token_family_id: UUID
    access_token_id: UUID
    refresh_token_id: UUID
    derivation_key_id: str
    refresh_generation: int
    access_issued_at: datetime
    access_expires_at: datetime
    refresh_expires_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class RefreshRotationCommand:
    """One refresh rotation's transactional writes (spec 13.4)."""

    predecessor_token_id: UUID
    predecessor_secret_hashes_by_key_id: dict[str, str] = field(repr=False)
    rotation_id: UUID
    successor_refresh_token_id: UUID
    successor_access_token_id: UUID
    successor_refresh_secret_hash: str = field(repr=False)
    successor_access_secret_hash: str = field(repr=False)
    derivation_key_id: str
    access_expires_at: datetime
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class CommittedRefreshRotation:
    """The committed — or replayed — successor of one rotation."""

    token_family_id: UUID
    successor_refresh_token_id: UUID
    successor_access_token_id: UUID
    successor_generation: int
    derivation_key_id: str
    rotated_at: datetime
    access_expires_at: datetime
    refresh_expires_at: datetime
    family_inactivity_expires_at: datetime
    family_absolute_expires_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class AccessTokenAuthenticationCommand:
    """One access-authentication lookup and state recheck (spec 13.1)."""

    token_id: UUID
    secret_hashes_by_key_id: dict[str, str] = field(repr=False)
    database_now: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedAccessToken:
    """One resolved access-token request context."""

    context: AuthenticatedDeviceContext
    database_now: datetime


@dataclass(frozen=True, slots=True)
class RevokeCurrentRefreshCommand:
    """One plugin self-revoke's transactional writes (spec 14.2)."""

    refresh_token_id: UUID
    secret_hashes_by_key_id: dict[str, str] = field(repr=False)
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class RevokedCurrentTokenFamily:
    """The committed terminal revoke of one presented family (spec 14.2)."""

    token_family_id: UUID
    device_id: UUID
    revoked_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class AdminRevokeDeviceCommand:
    """One Admin device revocation's transactional writes (spec 14.1)."""

    device_id: UUID
    workspace_id: UUID
    actor_user_id: UUID
    device_name_confirmation: str
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class AdminRevokedDevice:
    """The committed — or already committed — Admin revocation of one device."""

    device_id: UUID
    revoked_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class ListedAdminDevice:
    """One Admin device-list row: spec-approved fields only (spec 16.4, 18.3).

    Carries the display identity, the validated platform/plugin metadata of
    the exchanged grant, the lifecycle timestamps, the closed status and the
    family expiry; never a credential, a secret digest or a grant hash.
    """

    device_id: UUID
    device_name: str
    platform_class: str
    platform_name: str
    plugin_version: str
    status: str
    registered_at: datetime
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None
    approved_at: datetime | None = None
    family_absolute_expires_at: datetime | None = None


@runtime_checkable
class DeviceGrantExchangePort(Protocol):
    """The grant-side exchange transaction surface (spec 11.4, 12)."""

    async def poll_exchange(self, command: ExchangeGrantCommand) -> ExchangeProvisioning: ...


@runtime_checkable
class DeviceTokenTransactionPort(Protocol):
    """The token rotation, revoke and access-authentication surface (13, 14)."""

    async def resolve_refresh_predecessor(self, *, token_id: UUID) -> StoredDeviceToken | None: ...

    async def refresh_rotation(
        self, command: RefreshRotationCommand
    ) -> CommittedRefreshRotation: ...

    async def revoke_current_refresh(
        self, command: RevokeCurrentRefreshCommand
    ) -> RevokedCurrentTokenFamily: ...

    async def admin_revoke_device(
        self, command: AdminRevokeDeviceCommand
    ) -> AdminRevokedDevice: ...

    async def list_admin_devices(self, *, workspace_id: UUID) -> tuple[ListedAdminDevice, ...]: ...

    async def authenticate_access_token(
        self, command: AccessTokenAuthenticationCommand
    ) -> AuthenticatedAccessToken: ...


@runtime_checkable
class DeviceTokenKeyringPort(Protocol):
    """The versioned keyring view derivations anchor to (spec 20.1)."""

    def current_key_id(self) -> str: ...

    def keys_by_id(self) -> Mapping[str, bytes]: ...


# --- service results --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExchangedDeviceCredentials:
    """The exchanged credentials of one grant poll (spec 12.1, 12.2).

    The two credentials render under the provisioning cache-suppression
    headers; a replay re-renders the byte-identical values with the original
    anchored timestamps. ``repr`` suppresses both so no diagnostic sink can
    echo them.
    """

    grant_id: UUID
    device_id: UUID
    token_family_id: UUID
    refresh_generation: int
    access_credential: str = field(repr=False)
    refresh_credential: str = field(repr=False)
    access_expires_at: datetime
    refresh_expires_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class RefreshedDeviceCredentials:
    """The successor credentials of one refresh rotation (spec 13.3, 13.4)."""

    token_family_id: UUID
    refresh_generation: int
    access_credential: str = field(repr=False)
    refresh_credential: str = field(repr=False)
    access_expires_at: datetime
    refresh_expires_at: datetime
    family_absolute_expires_at: datetime
    database_now: datetime


# --- service -----------------------------------------------------------------------------


class DeviceTokenService:
    """The exchange, rotation and access choreography (spec 12, 13).

    One invocation of any method takes exactly one ``database_now`` read,
    derives and hashes every secret outside the transactions, and commits
    through the ports' single-purpose transactions. The exchange presents a
    polling credential whose digest selects the grant; a replay re-derives
    the committed credentials under the anchored key and lookup identities;
    the rotation pre-reads the predecessor to derive the successor hashes,
    then re-derives the response material from the committed outcome so an
    exact replay of a racing commit still renders byte-identical values.
    """

    def __init__(
        self,
        *,
        exchange: DeviceGrantExchangePort,
        tokens: DeviceTokenTransactionPort,
        keyring: DeviceTokenKeyringPort,
        crypto: AuthenticationCryptoPort,
        clock: AuthenticationClockPort,
        poll_pacer: GrantPollPacer | None = None,
    ) -> None:
        self._exchange = exchange
        self._tokens = tokens
        self._keyring = keyring
        self._clock = clock
        self._poll_pacer = poll_pacer if poll_pacer is not None else GrantPollPacer()
        self._grant_hmac_key = derive_grant_replay_hmac_key(
            crypto, keyring.keys_by_id()[keyring.current_key_id()]
        )

    # -- grant poll and exchange (spec 11.4, 12) ---------------------------------------

    async def exchange_grant(
        self,
        *,
        grant_id: UUID,
        polling_credential: str,
        diagnostic_context: DiagnosticContext,
    ) -> ExchangedDeviceCredentials:
        """Poll one grant; exchange it or replay the committed exchange.

        The presented polling credential is the only authority: its digest
        must select the named grant. A pending grant raises the closed
        pending outcome with the five-second hint — with the slow-down
        outcome once the pace window says the plugin polled faster —, a
        denied or expired grant raises its closed code, an approved grant
        commits the nine-step exchange of spec 12 once, and an exchanged
        grant re-derives the byte-identical credentials while the initial
        refresh generation is still the family's current generation.
        """
        parsed = parse_polling_credential(polling_credential)
        if parsed.grant_id != grant_id:
            raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
        database_now = await self._clock.database_now()
        keys_by_id = self._keyring.keys_by_id()
        key_id = self._keyring.current_key_id()
        master_key = keys_by_id[key_id]
        device_id = uuid7()
        token_family_id = uuid7()
        access_token_id = uuid7()
        refresh_token_id = uuid7()
        access_secret = derive_exchange_access_credential(
            master_key=master_key,
            polling_secret=parsed.secret,
            grant_id=grant_id,
            token_lookup_id=access_token_id,
        ).secret
        refresh_secret = derive_exchange_refresh_credential(
            master_key=master_key,
            polling_secret=parsed.secret,
            grant_id=grant_id,
            token_lookup_id=refresh_token_id,
        ).secret
        command = ExchangeGrantCommand(
            grant_id=grant_id,
            polling_secret_hash=polling_credential_hash_of(
                hmac_key=self._grant_hmac_key, polling_credential=polling_credential
            ),
            device_id=device_id,
            token_family_id=token_family_id,
            access_token_id=access_token_id,
            refresh_token_id=refresh_token_id,
            access_secret_hash=access_secret_hash_of(
                master_key=master_key, secret=_presented_segment(access_secret)
            ),
            refresh_secret_hash=refresh_secret_hash_of(
                master_key=master_key, secret=_presented_segment(refresh_secret)
            ),
            derivation_key_id=key_id,
            access_expires_at=database_now + ACCESS_TOKEN_LIFETIME,
            refresh_expires_at=database_now + REFRESH_INACTIVITY_LIFETIME,
            family_absolute_expires_at=database_now + REFRESH_ABSOLUTE_LIFETIME,
            database_now=database_now,
            diagnostic_context=diagnostic_context,
        )
        try:
            provisioned = await self._exchange.poll_exchange(command)
        except AuthenticationError as error:
            if error.error_code is ErrorCode.DEVICE_AUTHORIZATION_PENDING:
                self._raise_pending_or_slow_down(grant_id, error, database_now=database_now)
            raise
        return self._render_exchanged_credentials(parsed.secret, provisioned=provisioned)

    def _raise_pending_or_slow_down(
        self, grant_id: UUID, error: AuthenticationError, *, database_now: datetime
    ) -> None:
        """Convert one too-fast pending poll into the slow-down outcome."""
        retry_after_seconds = self._poll_pacer.register_pending_poll(grant_id, database_now)
        if retry_after_seconds is None:
            raise error
        raise AuthenticationError(
            ErrorCode.DEVICE_AUTHORIZATION_SLOW_DOWN,
            safe_details={"retry_after_seconds": retry_after_seconds},
        ) from error

    def _render_exchanged_credentials(
        self, polling_secret: bytes, *, provisioned: ExchangeProvisioning
    ) -> ExchangedDeviceCredentials:
        """Derive and render the exchange response from the committed outcome."""
        master_key = self._resolve_anchored_key(provisioned.derivation_key_id)
        access_secret = derive_exchange_access_credential(
            master_key=master_key,
            polling_secret=polling_secret,
            grant_id=provisioned.grant_id,
            token_lookup_id=provisioned.access_token_id,
            generation=provisioned.refresh_generation,
        ).secret
        refresh_secret = derive_exchange_refresh_credential(
            master_key=master_key,
            polling_secret=polling_secret,
            grant_id=provisioned.grant_id,
            token_lookup_id=provisioned.refresh_token_id,
            generation=provisioned.refresh_generation,
        ).secret
        return ExchangedDeviceCredentials(
            grant_id=provisioned.grant_id,
            device_id=provisioned.device_id,
            token_family_id=provisioned.token_family_id,
            refresh_generation=provisioned.refresh_generation,
            access_credential=format_access_credential(
                lookup_id=provisioned.access_token_id, secret=access_secret
            ),
            refresh_credential=format_refresh_credential(
                lookup_id=provisioned.refresh_token_id, secret=refresh_secret
            ),
            access_expires_at=provisioned.access_expires_at,
            refresh_expires_at=provisioned.refresh_expires_at,
            database_now=provisioned.database_now,
        )

    # -- refresh rotation (spec 13.3, 13.4) ---------------------------------------------

    async def refresh(
        self,
        *,
        refresh_credential: str,
        rotation_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> RefreshedDeviceCredentials:
        """Rotate the current refresh credential or replay the exact successor.

        The presented predecessor selects its row by lookup identity; the
        service derives the successor material outside the transaction under
        the current key, and the locked transaction classifies exact replay
        versus confirmed reuse versus a new rotation. The response is always
        re-derived from the committed outcome, so a retry after a lost commit
        acknowledgement renders the byte-identical successor and anchored
        timestamps.
        """
        parsed = parse_refresh_credential(refresh_credential)
        database_now = await self._clock.database_now()
        predecessor = await self._tokens.resolve_refresh_predecessor(token_id=parsed.lookup_id)
        if predecessor is None:
            raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
        keys_by_id = self._keyring.keys_by_id()
        key_id = self._keyring.current_key_id()
        master_key = keys_by_id[key_id]
        successor_generation = predecessor.generation + 1
        refresh_secret = derive_refresh_successor(
            master_key=master_key,
            predecessor_secret=parsed.secret,
            rotation_id=rotation_id,
            token_family_id=predecessor.token_family_id,
            successor_generation=successor_generation,
        ).secret
        access_secret = derive_rotation_access_credential(
            master_key=master_key,
            predecessor_secret=parsed.secret,
            rotation_id=rotation_id,
            token_family_id=predecessor.token_family_id,
            successor_generation=successor_generation,
        ).secret
        committed = await self._tokens.refresh_rotation(
            RefreshRotationCommand(
                predecessor_token_id=parsed.lookup_id,
                predecessor_secret_hashes_by_key_id={
                    candidate_key_id: refresh_secret_hash_of(
                        master_key=candidate_key, secret=parsed.secret
                    )
                    for candidate_key_id, candidate_key in keys_by_id.items()
                },
                rotation_id=rotation_id,
                successor_refresh_token_id=uuid7(),
                successor_access_token_id=uuid7(),
                successor_refresh_secret_hash=refresh_secret_hash_of(
                    master_key=master_key, secret=_presented_segment(refresh_secret)
                ),
                successor_access_secret_hash=access_secret_hash_of(
                    master_key=master_key, secret=_presented_segment(access_secret)
                ),
                derivation_key_id=key_id,
                access_expires_at=database_now + ACCESS_TOKEN_LIFETIME,
                database_now=database_now,
                diagnostic_context=diagnostic_context,
            )
        )
        return self._render_refreshed_credentials(
            parsed.secret, rotation_id=rotation_id, committed=committed
        )

    def _render_refreshed_credentials(
        self,
        predecessor_secret: bytes,
        *,
        rotation_id: UUID,
        committed: CommittedRefreshRotation,
    ) -> RefreshedDeviceCredentials:
        """Derive and render the rotation response from the committed outcome.

        The presented rotation identity is the committed successor's identity
        in both outcomes: a new rotation just committed it, and an exact
        replay was classified precisely because it matches.
        """
        master_key = self._resolve_anchored_key(committed.derivation_key_id)
        refresh_secret = derive_refresh_successor(
            master_key=master_key,
            predecessor_secret=predecessor_secret,
            rotation_id=rotation_id,
            token_family_id=committed.token_family_id,
            successor_generation=committed.successor_generation,
        ).secret
        access_secret = derive_rotation_access_credential(
            master_key=master_key,
            predecessor_secret=predecessor_secret,
            rotation_id=rotation_id,
            token_family_id=committed.token_family_id,
            successor_generation=committed.successor_generation,
        ).secret
        return RefreshedDeviceCredentials(
            token_family_id=committed.token_family_id,
            refresh_generation=committed.successor_generation,
            access_credential=format_access_credential(
                lookup_id=committed.successor_access_token_id, secret=access_secret
            ),
            refresh_credential=format_refresh_credential(
                lookup_id=committed.successor_refresh_token_id, secret=refresh_secret
            ),
            access_expires_at=committed.access_expires_at,
            refresh_expires_at=committed.refresh_expires_at,
            family_absolute_expires_at=committed.family_absolute_expires_at,
            database_now=committed.database_now,
        )

    # -- access authentication (spec 13.1) ----------------------------------------------

    async def authenticate_access(self, *, access_credential: str) -> AuthenticatedAccessToken:
        """Authenticate one access credential against the current state.

        The presented secret is hashed under every keyring key outside the
        transaction; the locked lookup verifies against the row's anchored
        key, then the token, family, device, user and workspace state at the
        single ``database_now``.
        """
        parsed = parse_access_credential(access_credential)
        database_now = await self._clock.database_now()
        return await self._tokens.authenticate_access_token(
            AccessTokenAuthenticationCommand(
                token_id=parsed.lookup_id,
                secret_hashes_by_key_id={
                    candidate_key_id: access_secret_hash_of(
                        master_key=candidate_key, secret=parsed.secret
                    )
                    for candidate_key_id, candidate_key in self._keyring.keys_by_id().items()
                },
                database_now=database_now,
            )
        )

    # -- plugin self-revoke (spec 14.2) -------------------------------------------------

    async def revoke_current(
        self,
        *,
        refresh_credential: str,
        diagnostic_context: DiagnosticContext,
    ) -> RevokedCurrentTokenFamily:
        """Revoke the family behind the presented current refresh credential.

        The presented secret is hashed under every keyring key outside the
        transaction; the locked transition verifies it against the row's
        anchored key, then revokes the family and every usable token in one
        commit with the terminal family-revoke audit row. A stale or already
        revoked credential surfaces the same closed terminal vocabulary the
        refresh route answers with, so the plugin tombstones either way.
        """
        parsed = parse_refresh_credential(refresh_credential)
        database_now = await self._clock.database_now()
        return await self._tokens.revoke_current_refresh(
            RevokeCurrentRefreshCommand(
                refresh_token_id=parsed.lookup_id,
                secret_hashes_by_key_id={
                    candidate_key_id: refresh_secret_hash_of(
                        master_key=candidate_key, secret=parsed.secret
                    )
                    for candidate_key_id, candidate_key in self._keyring.keys_by_id().items()
                },
                database_now=database_now,
                diagnostic_context=diagnostic_context,
            )
        )

    # -- internal helpers ----------------------------------------------------------------

    def _resolve_anchored_key(self, derivation_key_id: str) -> bytes:
        """Resolve the key that anchored one committed derivation (20.1).

        The startup keyring-coverage refusal makes a missing key corrupt
        internal state, which fails closed as the safe internal error.
        """
        master_key = self._keyring.keys_by_id().get(derivation_key_id)
        if master_key is None:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        return master_key


# --- the Admin device administration service (spec 14.1, 18.3) --------------------------


class DeviceAdministrationService:
    """The Admin device list and revocation choreography (spec 14.1).

    One invocation of any method resolves the presented Web session at its
    single ``database_now`` and enforces the fixed
    ``device_administration_manage`` scope of spec 6.1; the revocation adds
    the spec 9.4 recent re-authentication window before the locked store
    transition runs. The display-name confirmation and every state decision
    live in the transaction, so a mismatched name never revokes anything.
    """

    def __init__(
        self,
        *,
        tokens: DeviceTokenTransactionPort,
        session_service: SessionService,
        clock: AuthenticationClockPort,
    ) -> None:
        self._tokens = tokens
        self._session_service = session_service
        self._clock = clock

    async def list_devices(self, *, session_secret: str) -> tuple[ListedAdminDevice, ...]:
        """List the workspace's plugin devices behind the active session."""
        database_now = await self._clock.database_now()
        resolved = await self._session_service.resolve(
            session_secret=session_secret, database_now=database_now
        )
        self._require_administration_authority(resolved)
        return await self._tokens.list_admin_devices(workspace_id=resolved.context.workspace_id)

    async def revoke_device(
        self,
        *,
        device_id: UUID,
        session_secret: str,
        device_name_confirmation: str,
        diagnostic_context: DiagnosticContext,
    ) -> AdminRevokedDevice:
        """Revoke one device behind the recent re-authentication gate (14.1)."""
        database_now = await self._clock.database_now()
        resolved = await self._session_service.resolve(
            session_secret=session_secret, database_now=database_now
        )
        self._require_administration_authority(resolved)
        if not is_recently_authenticated(
            resolved.session,
            database_now=database_now,
            policy=self._session_service.session_policy,
        ):
            raise AuthenticationError(ErrorCode.RECENT_AUTHENTICATION_REQUIRED)
        return await self._tokens.admin_revoke_device(
            AdminRevokeDeviceCommand(
                device_id=device_id,
                workspace_id=resolved.context.workspace_id,
                actor_user_id=resolved.context.user_id,
                device_name_confirmation=device_name_confirmation,
                database_now=database_now,
                diagnostic_context=diagnostic_context,
            )
        )

    @staticmethod
    def _require_administration_authority(resolved: AuthenticatedSession) -> None:
        """Enforce the fixed administration scope of the active session (6.1)."""
        if WebScope.DEVICE_ADMINISTRATION_MANAGE not in resolved.context.scopes:
            raise AuthenticationError(ErrorCode.AUTHORIZATION_SCOPE_DENIED)


def _presented_segment(secret: bytes) -> bytes:
    """The secret bytes exactly as the formatted credential presents them.

    The stored digest and the verification digest must cover the identical
    presented segment, so both hash the hex spelling the credential string
    carries rather than the raw derived bytes.
    """
    return secret.hex().encode("ascii")


__all__ = [
    "ACCESS_TOKEN_LIFETIME",
    "ACCESS_TOKEN_LIFETIME_SECONDS",
    "ADMIN_REVOCATION_REASON",
    "DEVICE_LAST_SEEN_MAXIMUM_UPDATE_INTERVAL",
    "DEVICE_REVOKED_AUDIT_ACTION",
    "DEVICE_TOKEN_FAMILY_REVOKED_AUDIT_ACTION",
    "INITIAL_REFRESH_GENERATION",
    "REFRESH_ABSOLUTE_LIFETIME",
    "REFRESH_INACTIVITY_LIFETIME",
    "REFRESH_INACTIVITY_LIFETIME_SECONDS",
    "SELF_REVOCATION_REASON",
    "AccessTokenAuthenticationCommand",
    "AdminRevokeDeviceCommand",
    "AdminRevokedDevice",
    "AuthenticatedAccessToken",
    "CommittedRefreshRotation",
    "DeviceAdministrationService",
    "DeviceGrantExchangePort",
    "DeviceTokenKeyringPort",
    "DeviceTokenService",
    "DeviceTokenTransactionPort",
    "ExchangeGrantCommand",
    "ExchangeProvisioning",
    "ExchangedDeviceCredentials",
    "GrantPollPacer",
    "ListedAdminDevice",
    "ParsedPollingCredential",
    "RefreshPresentationKind",
    "RefreshRotationCommand",
    "RefreshedDeviceCredentials",
    "RevokeCurrentRefreshCommand",
    "RevokedCurrentTokenFamily",
    "StoredDeviceToken",
    "StoredTokenFamily",
    "access_secret_hash_of",
    "classify_refresh_presentation",
    "derive_exchange_access_credential",
    "derive_exchange_refresh_credential",
    "derive_refresh_successor",
    "derive_rotation_access_credential",
    "format_access_credential",
    "format_refresh_credential",
    "parse_polling_credential",
    "refresh_secret_hash_of",
]
