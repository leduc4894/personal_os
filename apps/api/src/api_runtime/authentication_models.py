"""Strict session/password/TOTP request and response models (spec 8-10, 16).

Every model is frozen and closed for extra fields. Password fields exist only
on request models, are bounded by the canonical 15-128 code-point policy and
never render in any response: the session payload carries exactly the
spec-9-named values a client needs — the closed session state, whether the
session authenticates right now, the granted scope set and the two expiry
hints — and never a username, cookie, credential or secret value. The
provisioning secret and the recovery codes render only in their intended
one-time responses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from personal_os.authentication.contracts import WebScope, WebSessionState
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
