"""Strict session/password/TOTP/device request and response models (spec 8-14, 16).

Every model is frozen and closed for extra fields. Password fields exist only
on request models, are bounded by the canonical 15-128 code-point policy and
never render in any response: the session payload carries exactly the
spec-9-named values a client needs — the closed session state, whether the
session authenticates right now, the granted scope set and the two expiry
hints — and never a username, cookie, credential or secret value. The
provisioning secret and the recovery codes render only in their intended
one-time responses; the device-grant payload renders the user code and the
polling secret exactly once at creation, and the approval-page context never
carries the polling secret at all.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from personal_os.authentication.contracts import (
    DeviceAuthorizationGrantState,
    DeviceScope,
    WebScope,
    WebSessionState,
)
from personal_os.authentication.device_authorization import (
    DEVICE_NAME_MAXIMUM_LENGTH_CHARACTERS,
    DEVICE_NAME_MINIMUM_LENGTH_CHARACTERS,
    PLATFORM_NAME_MAXIMUM_LENGTH_CHARACTERS,
    USER_CODE_ALPHABET,
    DevicePlatformClass,
)
from personal_os.authentication.passwords import (
    PASSWORD_MAXIMUM_LENGTH_CHARACTERS,
    PASSWORD_MINIMUM_LENGTH_CHARACTERS,
)
from personal_os.authentication.totp import TotpEnrollmentAction
from personal_os.identity.contracts import IDENTITY_KEY_PATTERN

#: The canonical username grammar, kept as one pattern string so the request
#: model and the identity contract render the same OpenAPI constraint.
_USERNAME_PATTERN: Final[str] = IDENTITY_KEY_PATTERN.pattern

#: The six-digit TOTP code grammar (spec 10.1).
_TOTP_CODE_PATTERN: Final[str] = r"^[0-9]{6}$"

#: The pasted recovery-code grammar: twelve Base32 characters in the grouped
#: spelling, with optional single separators (spec 10.3).
_RECOVERY_CODE_PATTERN: Final[str] = r"^[A-Za-z2-7]{4}([- ]?[A-Za-z2-7]{4}){2}$"

#: The closed user-code grammar (spec 11.1): two four-character blocks of
#: the unambiguous domain alphabet separated by exactly one hyphen.
_USER_CODE_PATTERN: Final[str] = rf"^[{USER_CODE_ALPHABET}]{{4}}-[{USER_CODE_ALPHABET}]{{4}}$"

#: The closed supported platform token grammar (spec 11.1).
_PLATFORM_NAME_PATTERN: Final[str] = r"^[a-z0-9]+([.-][a-z0-9]+)*$"

#: The semantic plugin version grammar (spec 11.1); character classes only,
#: because the exported document forbids backslash escapes in pattern strings.
_PLUGIN_VERSION_PATTERN: Final[str] = r"^[0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}$"

#: The closed actions a recovery-limited binding permits (spec 10.3).
TotpRecoveryPermittedAction = Literal["totp_replacement", "logout"]


class LoginRequest(BaseModel):
    """The strict username/password login body (spec 8.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    username: str = Field(pattern=_USERNAME_PATTERN)
    password: str = Field(
        min_length=PASSWORD_MINIMUM_LENGTH_CHARACTERS,
        max_length=PASSWORD_MAXIMUM_LENGTH_CHARACTERS,
    )


class ReauthenticateRequest(BaseModel):
    """The password body of one recent re-authentication attempt (spec 9.4).

    ``totp_code`` carries the second factor when the account holds an active
    TOTP credential: recent re-auth always verifies the password and also
    verifies TOTP when active, so a missing code fails like a wrong one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    password: str = Field(
        min_length=PASSWORD_MINIMUM_LENGTH_CHARACTERS,
        max_length=PASSWORD_MAXIMUM_LENGTH_CHARACTERS,
    )
    totp_code: str | None = Field(default=None, pattern=_TOTP_CODE_PATTERN)


class PasswordChangeRequest(BaseModel):
    """The new-password body of one password change (spec 9.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    new_password: str = Field(
        min_length=PASSWORD_MINIMUM_LENGTH_CHARACTERS,
        max_length=PASSWORD_MAXIMUM_LENGTH_CHARACTERS,
    )


class SessionData(BaseModel):
    """The public view of one Web session (spec 9.2).

    ``state`` covers the closed session-state vocabulary including ``revoked``
    (the logout response); ``authenticated`` is true only for an ``active``
    session, and the granted scopes stay empty in every non-active state so a
    client learns from the login response alone whether a TOTP or recovery
    challenge remains before any route authorizes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: WebSessionState
    authenticated: bool
    scopes: tuple[WebScope, ...]
    idle_expires_at: datetime
    absolute_expires_at: datetime


class TotpCodeRequest(BaseModel):
    """The six-digit code body of one TOTP challenge verification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=_TOTP_CODE_PATTERN)


class TotpEnrollmentRequest(BaseModel):
    """The strict discriminated enrollment action body (spec 10.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: TotpEnrollmentAction


class TotpRecoveryRequest(BaseModel):
    """The password plus one recovery code body (spec 10.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    password: str = Field(
        min_length=PASSWORD_MINIMUM_LENGTH_CHARACTERS,
        max_length=PASSWORD_MAXIMUM_LENGTH_CHARACTERS,
    )
    recovery_code: str = Field(pattern=_RECOVERY_CODE_PATTERN)


class TotpProofRequest(BaseModel):
    """The shared password plus current TOTP proof body (spec 10.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    password: str = Field(
        min_length=PASSWORD_MINIMUM_LENGTH_CHARACTERS,
        max_length=PASSWORD_MAXIMUM_LENGTH_CHARACTERS,
    )
    totp_code: str = Field(pattern=_TOTP_CODE_PATTERN)


class TotpEnrollmentOfferData(BaseModel):
    """The one-time provisioning material of a started enrollment (10.1).

    The provisioning URI and Base32 secret render exactly once, under the
    provisioning cache-suppression headers, and never again.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enrollment_id: UUID
    provisioning_uri: str
    secret: str
    expires_at: datetime


class TotpEnrollmentData(BaseModel):
    """The response payload of one enrollment action (spec 10.1).

    ``start`` carries the one-time offer; ``dismiss_initial_offer`` carries
    only the recorded dismissal moment — never a secret or pending row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: TotpEnrollmentAction
    enrollment: TotpEnrollmentOfferData | None = None
    dismissed_at: datetime | None = None


class RecoveryCodesData(BaseModel):
    """One recovery-code revision displayed exactly once (spec 10.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    codes: tuple[str, ...]
    revision: int


class RecoveryLimitedContext(BaseModel):
    """The recovery-limited binding context one accepted recovery produces.

    The closed permitted-action set tells the client only TOTP replacement or
    logout remains before normal Admin access returns (spec 10.3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: WebSessionState
    permitted_actions: tuple[TotpRecoveryPermittedAction, ...]
    idle_expires_at: datetime
    absolute_expires_at: datetime


class DeviceGrantRequest(BaseModel):
    """The strict unauthenticated plugin grant-creation body (spec 11.1).

    ``client_instance_id`` is the non-secret UUID the plugin generated once;
    ``claimed_device_id`` optionally carries one prior non-secret device id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_instance_id: UUID
    device_name: str = Field(
        min_length=DEVICE_NAME_MINIMUM_LENGTH_CHARACTERS,
        max_length=DEVICE_NAME_MAXIMUM_LENGTH_CHARACTERS,
    )
    platform_class: DevicePlatformClass
    platform_name: str = Field(
        pattern=_PLATFORM_NAME_PATTERN, max_length=PLATFORM_NAME_MAXIMUM_LENGTH_CHARACTERS
    )
    plugin_version: str = Field(pattern=_PLUGIN_VERSION_PATTERN)
    requested_scope: DeviceScope
    claimed_device_id: UUID | None = None


class DeviceGrantData(BaseModel):
    """The one-time provisioning payload of one created grant (spec 11.1).

    The user code and polling secret render exactly once here, under the
    provisioning cache-suppression headers, and never again.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: UUID
    user_code: str
    polling_secret: str
    verification_uri: str
    verification_uri_complete: str
    expires_in_seconds: int
    poll_interval_seconds: int


class DeviceGrantLookupRequest(BaseModel):
    """The user-code body the approval page resolves a grant with (11.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_code: str = Field(pattern=_USER_CODE_PATTERN)


class DeviceGrantContextData(BaseModel):
    """The approval-page display context of one pending grant (spec 11.3).

    Carries exactly the values the page must show before any decision: the
    same user code the plugin displays, the escaped device name, the platform
    class and token, the validated plugin version, the fixed scope and the
    expiry. The polling secret never appears.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: UUID
    user_code: str
    device_name: str
    platform_class: DevicePlatformClass
    platform_name: str
    plugin_version: str
    requested_scope: DeviceScope
    expires_at: datetime


class DeviceGrantDecisionData(BaseModel):
    """The committed terminal decision of one approve/deny action (11.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: UUID
    state: DeviceAuthorizationGrantState
    decided_at: datetime


class DeviceGrantExchangeData(BaseModel):
    """The exchanged device credentials of one grant poll (spec 12.1, 12.2).

    The access and refresh credentials render under the provisioning
    cache-suppression headers; an exact replay after a lost acknowledgement
    re-renders the byte-identical values with the original anchored
    timestamps.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: UUID
    device_id: UUID
    token_family_id: UUID
    refresh_generation: int
    access_credential: str
    refresh_credential: str
    access_expires_at: datetime
    refresh_expires_at: datetime


class DeviceRefreshRequest(BaseModel):
    """The strict rotation body of one refresh presentation (spec 13.4).

    ``rotation_id`` is the plugin-owned UUID retry identity: one stable
    identity replays the exact committed successor, a new identity on a
    rotated predecessor is confirmed reuse.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rotation_id: UUID


class RefreshedDeviceTokenData(BaseModel):
    """The successor credentials of one refresh rotation (spec 13.3, 13.4).

    The access and refresh credentials render under the provisioning
    cache-suppression headers; an exact replay re-renders the byte-identical
    successor with its original anchored timestamps and never extends the
    family's absolute expiry.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_family_id: UUID
    refresh_generation: int
    access_credential: str
    refresh_credential: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    family_absolute_expires_at: datetime


class DeviceSelfRevokeData(BaseModel):
    """The confirmed terminal revoke of one plugin self-revoke (spec 14.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: UUID
    token_family_id: UUID
    revoked_at: datetime


#: The closed device lifecycle status vocabulary of the Admin list (18.3).
DeviceLifecycleStatus = Literal["active", "revoked"]


class AdminDeviceData(BaseModel):
    """One Admin device-list row: spec-approved fields only (16.4, 18.3).

    Carries the display identity, the Desktop/Mobile class, the platform, the
    validated plugin version, the closed lifecycle status, the
    registered/last-seen/revoked moments and the family expiry; never a
    credential, hash or polling identity.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: UUID
    device_name: str
    platform_class: DevicePlatformClass
    platform_name: str
    plugin_version: str
    status: DeviceLifecycleStatus
    registered_at: datetime
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None
    family_absolute_expires_at: datetime | None = None


class AdminDeviceListData(BaseModel):
    """The Admin device list of one workspace (spec 16.4, 18.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    devices: tuple[AdminDeviceData, ...]


class AdminDeviceRevokeRequest(BaseModel):
    """The exact display-name confirmation body of one Admin revoke (14.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_name_confirmation: str = Field(
        min_length=DEVICE_NAME_MINIMUM_LENGTH_CHARACTERS,
        max_length=DEVICE_NAME_MAXIMUM_LENGTH_CHARACTERS,
    )


class AdminDeviceRevokeData(BaseModel):
    """The committed — or already committed — Admin revocation (spec 14.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: UUID
    revoked_at: datetime
