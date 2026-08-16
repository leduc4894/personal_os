"""Authentication-domain error bound to the closed sixteen-code registry.

The closed code set is exactly the authentication block of the error registry
(spec 17). The rejected input of a validation or parsing failure — a password,
a credential string, a user code — is never retained on the exception, so
``str``, ``repr`` and :meth:`to_safe_dict` can only ever expose the registry
code, category, retryability and registered safe details.
"""

from __future__ import annotations

from typing import Final

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError

#: The closed authentication code set (spec 17): every code this domain raises.
AUTHENTICATION_ERROR_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.AUTHENTICATION_REQUIRED,
        ErrorCode.AUTHENTICATION_FAILED,
        ErrorCode.AUTHENTICATION_RATE_LIMITED,
        ErrorCode.RECENT_AUTHENTICATION_REQUIRED,
        ErrorCode.CSRF_VALIDATION_FAILED,
        ErrorCode.AUTHORIZATION_SCOPE_DENIED,
        ErrorCode.TOTP_ENROLLMENT_STATE_INVALID,
        ErrorCode.DEVICE_AUTHORIZATION_PENDING,
        ErrorCode.DEVICE_AUTHORIZATION_SLOW_DOWN,
        ErrorCode.DEVICE_AUTHORIZATION_DENIED,
        ErrorCode.DEVICE_AUTHORIZATION_EXPIRED,
        ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID,
        ErrorCode.DEVICE_CREDENTIAL_INVALID,
        ErrorCode.DEVICE_REVOKED,
        ErrorCode.DEVICE_TOKEN_REUSE_DETECTED,
        ErrorCode.PLUGIN_VERSION_UNSUPPORTED,
    }
)


class AuthenticationError(ApplicationError):
    """Typed authentication error over the closed authentication code set.

    Constructors never accept rejected input values; the base class guarantees
    only registered safe detail fields survive serialization.
    """

    allowed_codes: frozenset[ErrorCode] = AUTHENTICATION_ERROR_CODES
