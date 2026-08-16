"""Opaque device-credential parsing and HKDF domain-separation labels.

Credentials are opaque and versioned (spec 12.1): ``at1.<lookup_id>.<secret>``
and ``rt1.<lookup_id>.<secret>``. The parsers return typed
:class:`~personal_os.authentication.contracts.OpaqueCredential` values or
raise :class:`~personal_os.authentication.errors.AuthenticationError` with the
generic ``device_credential_invalid`` code. Unknown versions, wrong kinds,
wrong segment counts, invalid lookup identifiers and size violations are all
rejected through one failure path that never retains or echoes the rejected
value — not in the exception, its ``str``/``repr`` or its safe details.

The HKDF domain labels of spec 20.1 — TOTP-secret authenticated encryption,
CSRF hashing, throttle-bucket HMAC, recovery-code hashing, the grant/exchange
exact-replay derivations and the access/refresh credential derivations — are
pinned to exact bytes here as the closed
vocabulary every subkey derivation must name; deriving with a label outside
this set is a contract violation, not a configuration choice.
"""

from __future__ import annotations

import re
from typing import Final
from uuid import UUID

from personal_os.authentication.contracts import DeviceTokenKind, OpaqueCredential
from personal_os.authentication.errors import AuthenticationError
from personal_os.error_contracts.codes import ErrorCode

#: CSRF token hashing domain (spec 9.3, 20.1).
CSRF_HASH_LABEL: Final[str] = "auth/csrf/v1"

#: Throttle-bucket HMAC domain (spec 8.3, 20.1).
THROTTLE_HMAC_LABEL: Final[str] = "auth/throttle/v1"

#: TOTP-secret authenticated-encryption domain (spec 10.1, 20.1).
TOTP_SECRET_AEAD_LABEL: Final[str] = "auth/totp-secret/v1"

#: Recovery-code hashing domain (spec 10.3, 20.1).
RECOVERY_CODE_HASH_LABEL: Final[str] = "auth/recovery/v1"

#: Grant and initial token exact-replay derivation domain (spec 12.2, 20.1).
GRANT_REPLAY_DERIVATION_LABEL: Final[str] = "auth/grant-replay/v1"

#: Refresh rotation exact-replay derivation domain (spec 13.4, 20.1).
REFRESH_REPLAY_DERIVATION_LABEL: Final[str] = "auth/refresh-replay/v1"

#: Initial-exchange refresh-credential derivation domain (spec 12.2, 20.1).
EXCHANGE_CREDENTIAL_DERIVATION_LABEL: Final[str] = "auth/exchange-credential/v1"

#: Access-credential derivation and verification domain (spec 12.2, 13.1, 20.1).
ACCESS_CREDENTIAL_DERIVATION_LABEL: Final[str] = "auth/access-credential/v1"

#: The closed domain-separation label vocabulary (exact bytes).
CRYPTO_DOMAIN_LABELS: Final[frozenset[str]] = frozenset(
    {
        TOTP_SECRET_AEAD_LABEL,
        CSRF_HASH_LABEL,
        THROTTLE_HMAC_LABEL,
        RECOVERY_CODE_HASH_LABEL,
        GRANT_REPLAY_DERIVATION_LABEL,
        REFRESH_REPLAY_DERIVATION_LABEL,
        EXCHANGE_CREDENTIAL_DERIVATION_LABEL,
        ACCESS_CREDENTIAL_DERIVATION_LABEL,
    }
)

#: Versioned credential prefixes (spec 12.1).
ACCESS_CREDENTIAL_PREFIX: Final[str] = "at1"
REFRESH_CREDENTIAL_PREFIX: Final[str] = "rt1"

#: Structural credential bounds: three dot-separated segments.
CREDENTIAL_SEGMENT_COUNT: Final[int] = 3

#: Whole-credential bound in characters; anything longer is a size violation.
CREDENTIAL_MAXIMUM_LENGTH_CHARACTERS: Final[int] = 512

#: Secret-segment bounds in characters. Real secrets are derived 32-byte
#: values (43 base64url or 64 hex characters); the floor rejects degenerate
#: values and the ceiling bounds hostile input before any hashing.
CREDENTIAL_SECRET_MINIMUM_LENGTH_CHARACTERS: Final[int] = 16
CREDENTIAL_SECRET_MAXIMUM_LENGTH_CHARACTERS: Final[int] = 128

#: URL-safe opaque secret grammar: ASCII token characters only.
_CREDENTIAL_SECRET_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")

#: Canonical lowercase hyphenated lookup identifier: 8-4-4-4-12 hex.
_CREDENTIAL_LOOKUP_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _invalid_credential() -> AuthenticationError:
    """Build the credential rejection carrying no trace of the input."""
    return AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)


def _parse_opaque_credential(value: str, expected_prefix: str) -> OpaqueCredential:
    """Parse one opaque credential or fail closed without echoing the input."""
    if not isinstance(value, str) or not value or len(value) > CREDENTIAL_MAXIMUM_LENGTH_CHARACTERS:
        raise _invalid_credential()
    segments = value.split(".")
    if len(segments) != CREDENTIAL_SEGMENT_COUNT:
        raise _invalid_credential()
    version_segment, lookup_id_segment, secret_segment = segments
    if version_segment != expected_prefix:
        raise _invalid_credential()
    if _CREDENTIAL_LOOKUP_ID_PATTERN.fullmatch(lookup_id_segment) is None:
        raise _invalid_credential()
    if not (
        CREDENTIAL_SECRET_MINIMUM_LENGTH_CHARACTERS
        <= len(secret_segment)
        <= CREDENTIAL_SECRET_MAXIMUM_LENGTH_CHARACTERS
        and _CREDENTIAL_SECRET_PATTERN.fullmatch(secret_segment) is not None
    ):
        raise _invalid_credential()
    return OpaqueCredential(
        token_kind=DeviceTokenKind.ACCESS
        if expected_prefix == ACCESS_CREDENTIAL_PREFIX
        else DeviceTokenKind.REFRESH,
        lookup_id=UUID(lookup_id_segment),
        secret=secret_segment.encode("ascii"),
    )


def parse_access_credential(value: str) -> OpaqueCredential:
    """Parse one ``at1.<lookup_id>.<secret>`` access credential."""
    return _parse_opaque_credential(value, ACCESS_CREDENTIAL_PREFIX)


def parse_refresh_credential(value: str) -> OpaqueCredential:
    """Parse one ``rt1.<lookup_id>.<secret>`` refresh credential."""
    return _parse_opaque_credential(value, REFRESH_CREDENTIAL_PREFIX)
