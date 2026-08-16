"""Closed authentication state vocabularies, scopes and authenticated contexts.

Every enum member is exactly a state the design spec names and later tasks
consume: session states (spec 9.2), TOTP credential states (spec 10.1/15.3),
device authorization grant states (spec 11.4/12/15.5), device token kinds,
token states and family states (spec 13/15.6/15.7) and the Web/device scopes
(spec 6). The authenticated contexts carry ids, scopes and revision anchors
only — never a credential, a username or any secret-bearing value.

The modules of this package import no infrastructure SDK, composition root,
web framework or crypto implementation package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from uuid import UUID


class WebSessionState(StrEnum):
    """Closed Web session states (spec 9.2)."""

    PENDING_TOTP = "pending_totp"
    ACTIVE = "active"
    RECOVERY_LIMITED = "recovery_limited"
    REVOKED = "revoked"


class TotpCredentialState(StrEnum):
    """Closed TOTP credential states (spec 10.1, 15.3).

    ``pending`` is an enrollment awaiting its first verified code,
    ``active`` is the single verified credential and ``replaced`` marks a
    credential superseded by replacement or disable.
    """

    PENDING = "pending"
    ACTIVE = "active"
    REPLACED = "replaced"


class DeviceAuthorizationGrantState(StrEnum):
    """Closed browser device-authorization grant states (spec 11.4/12, 15.5).

    Grant expiry is decided against ``expires_at`` while pending; it is not a
    stored state.
    """

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXCHANGED = "exchanged"


class DeviceTokenKind(StrEnum):
    """Closed opaque device credential kinds (spec 12.1, 15.7)."""

    ACCESS = "access"
    REFRESH = "refresh"


class DeviceTokenState(StrEnum):
    """Closed device token states (spec 13.4/13.5, 15.7).

    Token expiry is decided against ``expires_at``; a usable credential is in
    ``active`` state only.
    """

    ACTIVE = "active"
    ROTATED = "rotated"
    REVOKED = "revoked"


class DeviceTokenFamilyState(StrEnum):
    """Closed device token family states (spec 13.5, 15.6)."""

    ACTIVE = "active"
    REVOKED = "revoked"


class WebScope(StrEnum):
    """The fixed Phase 2 Web administration scopes (spec 6.1).

    A Web session always carries the full surface; the client never chooses,
    widens or customizes these values.
    """

    WEB_SECURITY_MANAGE = "web_security_manage"
    DEVICE_AUTHORIZATION_APPROVE = "device_authorization_approve"
    DEVICE_ADMINISTRATION_MANAGE = "device_administration_manage"


class DeviceScope(StrEnum):
    """The closed fixed Obsidian device scope (spec 6.2)."""

    OBSIDIAN_SYNC = "obsidian_sync"


#: Every Web scope granted by an authenticated Web session (spec 6.1).
AUTHENTICATED_WEB_SCOPES: Final[frozenset[WebScope]] = frozenset(WebScope)

#: The one fixed scope an approved device token carries (spec 6.2).
FIXED_DEVICE_SCOPE: Final[DeviceScope] = DeviceScope.OBSIDIAN_SYNC


@dataclass(frozen=True, slots=True)
class OpaqueCredential:
    """One parsed opaque versioned device credential (spec 12.1).

    ``lookup_id`` is the non-secret row selector and ``secret`` is the raw
    secret segment bytes; PostgreSQL ever stores only a hash of the secret.
    The secret never renders: ``repr`` hides it so no diagnostic sink or error
    rendering can echo it.
    """

    token_kind: DeviceTokenKind
    lookup_id: UUID
    secret: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuthenticatedWebContext:
    """Resolved Web identity of one authenticated session request (spec 6.1, 9).

    Carries the canonical ids, the session row identity, the credential
    revision anchor for staleness checks and the fixed granted scopes; no
    credential, cookie, username or CSRF material.
    """

    user_id: UUID
    workspace_id: UUID
    web_session_id: UUID
    credential_revision: int
    scopes: frozenset[WebScope]


@dataclass(frozen=True, slots=True)
class AuthenticatedDeviceContext:
    """Resolved Obsidian identity of one access-token-authenticated request.

    Carries exactly the four values an approved device token resolves
    (spec 6.2): user, workspace, device and the fixed ``obsidian_sync`` scope.
    """

    user_id: UUID
    workspace_id: UUID
    device_id: UUID
    scope: DeviceScope
