"""Public authentication domain contracts.

Closed state vocabularies, scopes and authenticated contexts, the typed
authentication error over the closed sixteen-code registry, the
provider-neutral password-hashing and crypto ports, the password policy with
the offline common-password blocklist, and the opaque device-credential
parser with the HKDF domain-separation labels. The modules import no
infrastructure SDK, composition root, web framework or crypto implementation
package; the concrete adapters live in the API composition root.
"""

from personal_os.authentication.contracts import (
    AUTHENTICATED_WEB_SCOPES,
    FIXED_DEVICE_SCOPE,
    AuthenticatedDeviceContext,
    AuthenticatedWebContext,
    DeviceAuthorizationGrantState,
    DeviceScope,
    DeviceTokenFamilyState,
    DeviceTokenKind,
    DeviceTokenState,
    OpaqueCredential,
    TotpCredentialState,
    WebScope,
    WebSessionState,
)
from personal_os.authentication.crypto import (
    ACCESS_CREDENTIAL_PREFIX,
    CREDENTIAL_MAXIMUM_LENGTH_CHARACTERS,
    CREDENTIAL_SECRET_MAXIMUM_LENGTH_CHARACTERS,
    CREDENTIAL_SECRET_MINIMUM_LENGTH_CHARACTERS,
    CREDENTIAL_SEGMENT_COUNT,
    CRYPTO_DOMAIN_LABELS,
    CSRF_HASH_LABEL,
    GRANT_REPLAY_DERIVATION_LABEL,
    RECOVERY_CODE_HASH_LABEL,
    REFRESH_CREDENTIAL_PREFIX,
    REFRESH_REPLAY_DERIVATION_LABEL,
    THROTTLE_HMAC_LABEL,
    parse_access_credential,
    parse_refresh_credential,
)
from personal_os.authentication.errors import (
    AUTHENTICATION_ERROR_CODES,
    AuthenticationError,
)
from personal_os.authentication.passwords import (
    ARGON2ID_HASH_LENGTH_BYTES,
    ARGON2ID_MEMORY_COST_KIB,
    ARGON2ID_PARALLELISM_LANES,
    ARGON2ID_SALT_LENGTH_BYTES,
    ARGON2ID_TIME_COST_ITERATIONS,
    COMMON_PASSWORD_BLOCKLIST_DIGEST_COUNT,
    COMMON_PASSWORD_BLOCKLIST_RESOURCE_NAME,
    PASSWORD_MAXIMUM_LENGTH_CHARACTERS,
    PASSWORD_MINIMUM_LENGTH_CHARACTERS,
    PasswordBlocklist,
    load_common_password_blocklist,
    validate_new_password,
)
from personal_os.authentication.ports import (
    AuthenticationCryptoPort,
    PasswordHasherPort,
)

__all__ = [
    "ACCESS_CREDENTIAL_PREFIX",
    "ARGON2ID_HASH_LENGTH_BYTES",
    "ARGON2ID_MEMORY_COST_KIB",
    "ARGON2ID_PARALLELISM_LANES",
    "ARGON2ID_SALT_LENGTH_BYTES",
    "ARGON2ID_TIME_COST_ITERATIONS",
    "AUTHENTICATED_WEB_SCOPES",
    "AUTHENTICATION_ERROR_CODES",
    "COMMON_PASSWORD_BLOCKLIST_DIGEST_COUNT",
    "COMMON_PASSWORD_BLOCKLIST_RESOURCE_NAME",
    "CREDENTIAL_MAXIMUM_LENGTH_CHARACTERS",
    "CREDENTIAL_SECRET_MAXIMUM_LENGTH_CHARACTERS",
    "CREDENTIAL_SECRET_MINIMUM_LENGTH_CHARACTERS",
    "CREDENTIAL_SEGMENT_COUNT",
    "CRYPTO_DOMAIN_LABELS",
    "CSRF_HASH_LABEL",
    "FIXED_DEVICE_SCOPE",
    "GRANT_REPLAY_DERIVATION_LABEL",
    "PASSWORD_MAXIMUM_LENGTH_CHARACTERS",
    "PASSWORD_MINIMUM_LENGTH_CHARACTERS",
    "RECOVERY_CODE_HASH_LABEL",
    "REFRESH_CREDENTIAL_PREFIX",
    "REFRESH_REPLAY_DERIVATION_LABEL",
    "THROTTLE_HMAC_LABEL",
    "AuthenticatedDeviceContext",
    "AuthenticatedWebContext",
    "AuthenticationCryptoPort",
    "AuthenticationError",
    "DeviceAuthorizationGrantState",
    "DeviceScope",
    "DeviceTokenFamilyState",
    "DeviceTokenKind",
    "DeviceTokenState",
    "OpaqueCredential",
    "PasswordBlocklist",
    "PasswordHasherPort",
    "TotpCredentialState",
    "WebScope",
    "WebSessionState",
    "load_common_password_blocklist",
    "parse_access_credential",
    "parse_refresh_credential",
    "validate_new_password",
]
