"""Strict session/password request and response models (spec 8.1, 9, 16.1).

Every model is frozen and closed for extra fields. Password fields exist only
on request models, are bounded by the canonical 15-128 code-point policy and
never render in any response: the session payload carries exactly the
spec-9-named values a client needs — the closed session state, whether the
session authenticates right now, the granted scope set and the two expiry
hints — and never a username, cookie, credential or secret value.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from personal_os.authentication.contracts import WebScope, WebSessionState
from personal_os.authentication.passwords import (
    PASSWORD_MAXIMUM_LENGTH_CHARACTERS,
    PASSWORD_MINIMUM_LENGTH_CHARACTERS,
)
from personal_os.identity.contracts import IDENTITY_KEY_PATTERN

#: The canonical username grammar, kept as one pattern string so the request
#: model and the identity contract render the same OpenAPI constraint.
_USERNAME_PATTERN: Final[str] = IDENTITY_KEY_PATTERN.pattern


class LoginRequest(BaseModel):
    """The strict username/password login body (spec 8.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    username: str = Field(pattern=_USERNAME_PATTERN)
    password: str = Field(
        min_length=PASSWORD_MINIMUM_LENGTH_CHARACTERS,
        max_length=PASSWORD_MAXIMUM_LENGTH_CHARACTERS,
    )


class ReauthenticateRequest(BaseModel):
    """The password body of one recent re-authentication attempt (spec 9.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    password: str = Field(
        min_length=PASSWORD_MINIMUM_LENGTH_CHARACTERS,
        max_length=PASSWORD_MAXIMUM_LENGTH_CHARACTERS,
    )


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
