"""Fail-before-bind authentication keyring loading from exact secret files.

This module loads the bounded ordered keyring of versioned 32-byte
authentication master keys (spec 20.1). Every key file named by the
authentication settings is read through the existing secret-file boundary, so
missing, out-of-root, wrong-type, oversized or permission-unsafe files keep
their registered ``secret_file_*`` codes and refuse startup before socket
exposure. Key material must be exactly 64 hexadecimal characters (32 bytes);
anything else fails as ``configuration_secret_invalid`` without echoing the
file name or material.

It also hosts the concrete adapters implementing the framework-neutral
authentication ports: :class:`Argon2PasswordHasher` binds ``argon2-cffi`` to
the pinned Argon2id domain parameters and
:class:`CryptographyAuthenticationCrypto` binds ``cryptography`` to the
HKDF-SHA-256, HMAC-SHA-256 and AES-256-GCM contracts. Only this composition
root imports either package; the domain pins constants and protocols only.

This module lives in the API composition-root package and never imports
FastAPI or Uvicorn, so the shell-only command paths stay free of server
machinery and never read a key file.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, NoReturn

import argon2
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

from api_runtime.authentication_settings import (
    AUTHENTICATION_KEY_SIZE_BYTES,
    AuthenticationSettings,
)
from personal_os.authentication.crypto import assert_crypto_domain_label
from personal_os.authentication.passwords import (
    ARGON2ID_HASH_LENGTH_BYTES,
    ARGON2ID_MEMORY_COST_KIB,
    ARGON2ID_PARALLELISM_LANES,
    ARGON2ID_SALT_LENGTH_BYTES,
    ARGON2ID_TIME_COST_ITERATIONS,
)
from personal_os.authentication.ports import (
    AuthenticationCryptoPort,
    PasswordHasherPort,
)
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import (
    ConfigurationError,
    InternalApplicationError,
)
from personal_os.runtime_configuration.secret_files import read_secret_file

#: Key material grammar: hexadecimal characters only. The separate length check
#: pins the decoded size to exactly 32 bytes.
_KEY_MATERIAL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]+$")

#: HKDF-SHA-256 salt: 32 zero bytes (the RFC 5869 default salt length), so
#: derivation depends only on the master key and the explicit domain label.
_HKDF_SALT_BYTES: Final[bytes] = bytes(AUTHENTICATION_KEY_SIZE_BYTES)

#: AES-GCM nonce length: exactly 12 fresh bytes per seal operation.
_AEAD_NONCE_SIZE_BYTES: Final[int] = 12

#: Domain-label bounds: ASCII, one through 64 characters.
_DOMAIN_LABEL_MINIMUM_LENGTH: Final[int] = 1
_DOMAIN_LABEL_MAXIMUM_LENGTH: Final[int] = 64


@dataclass(frozen=True, slots=True)
class AuthenticationKeyring:
    """Immutable view of the loaded versioned master keys keyed by key ID."""

    current_key_id: str
    keys_by_id: Mapping[str, bytes]

    def current_key(self) -> bytes:
        return self.keys_by_id[self.current_key_id]


def _invalid_key_error(reason: str) -> ConfigurationError:
    """Build the key-material failure carrying only a safe ``reason`` token."""
    return ConfigurationError(
        ErrorCode.CONFIGURATION_SECRET_INVALID,
        safe_details={"reason": SafeToken.parse(reason)},
    )


def _decode_key_material(secret: SecretStr) -> bytes:
    """Decode one secret value into exactly 32 bytes of key material."""
    value = secret.get_secret_value()
    if _KEY_MATERIAL_PATTERN.fullmatch(value) is None:
        raise _invalid_key_error("invalid_encoding")
    try:
        key_bytes = bytes.fromhex(value)
    except ValueError as cause:
        raise _invalid_key_error("invalid_encoding") from cause
    if len(key_bytes) != AUTHENTICATION_KEY_SIZE_BYTES:
        raise _invalid_key_error("wrong_length")
    return key_bytes


def _load_key_bytes(secret_root: Path, file_name: str) -> bytes:
    secret = read_secret_file(secret_root / file_name, secret_root)
    return _decode_key_material(secret)


def load_authentication_keyring(settings: AuthenticationSettings) -> AuthenticationKeyring:
    """Load the ordered keyring: previous keys first, the current key last.

    Every referenced file is read through the secret-file boundary; a boundary
    failure (missing file, escape attempt, unsafe permission) propagates its
    own registered :class:`SecretFileError` and malformed key material raises
    :class:`ConfigurationError` with ``configuration_secret_invalid``. The
    returned keyring shares no mutable state: ``keys_by_id`` is an immutable
    mapping over fresh ``bytes`` values.
    """
    keys_by_id: dict[str, bytes] = {}
    for key_id, file_name in settings.previous_key_files:
        keys_by_id[key_id] = _load_key_bytes(settings.secret_root, file_name)
    keys_by_id[settings.current_key_id] = _load_key_bytes(
        settings.secret_root,
        settings.current_key_file,
    )
    return AuthenticationKeyring(
        current_key_id=settings.current_key_id,
        keys_by_id=MappingProxyType(keys_by_id),
    )


def _internal_failure(cause: BaseException) -> NoReturn:
    """Raise the fail-closed internal error without crypto or hash text."""
    raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause


class Argon2PasswordHasher(PasswordHasherPort):
    """Argon2id adapter over the pinned domain parameters (spec 8.1).

    Hashes are encoded PHC strings carrying algorithm and work parameters, so
    :meth:`needs_rehash` reports obsolete parameters for a successful login to
    upgrade. A mismatch is a plain ``False``; a malformed stored hash is
    corrupt internal state and fails closed as the safe ``internal_error``
    without echoing the stored value.
    """

    def __init__(self) -> None:
        self._hasher = argon2.PasswordHasher(
            type=argon2.Type.ID,
            memory_cost=ARGON2ID_MEMORY_COST_KIB,
            time_cost=ARGON2ID_TIME_COST_ITERATIONS,
            parallelism=ARGON2ID_PARALLELISM_LANES,
            salt_len=ARGON2ID_SALT_LENGTH_BYTES,
            hash_len=ARGON2ID_HASH_LENGTH_BYTES,
        )

    def hash_password(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify_password(self, password_hash: str, password: str) -> bool:
        try:
            self._hasher.verify(password_hash, password)
        except argon2.exceptions.VerifyMismatchError:
            return False
        except argon2.exceptions.VerificationError:
            return False
        except argon2.exceptions.InvalidHashError as cause:
            _internal_failure(cause)
        return True

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)


class CryptographyAuthenticationCrypto(AuthenticationCryptoPort):
    """AES-256-GCM / HKDF-SHA-256 / HMAC-SHA-256 adapter (spec 20.1).

    Every subkey is derived through RFC 5869 HKDF-SHA-256 with a 32-byte zero
    salt and one explicit ASCII domain label; AES-GCM seals use a fresh
    12-byte nonce per operation, and any decrypt, tag or parameter failure
    fails closed as the safe ``internal_error`` without crypto text.
    """

    def derive_subkey(self, *, master_key: bytes, label: str) -> bytes:
        if len(master_key) != AUTHENTICATION_KEY_SIZE_BYTES:
            _internal_failure(ValueError("master key size"))
        try:
            info = label.encode("ascii")
        except UnicodeEncodeError as cause:
            _internal_failure(cause)
        if not _DOMAIN_LABEL_MINIMUM_LENGTH <= len(info) <= _DOMAIN_LABEL_MAXIMUM_LENGTH:
            _internal_failure(ValueError("domain label length"))
        assert_crypto_domain_label(label)
        return HKDF(
            algorithm=hashes.SHA256(),
            length=AUTHENTICATION_KEY_SIZE_BYTES,
            salt=_HKDF_SALT_BYTES,
            info=info,
        ).derive(master_key)

    def hmac_sha256(self, *, key: bytes, message: bytes) -> bytes:
        return hmac.new(key, message, hashlib.sha256).digest()

    def seal_secret(self, *, key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
        if len(key) != AUTHENTICATION_KEY_SIZE_BYTES:
            _internal_failure(ValueError("aes key size"))
        nonce = secrets.token_bytes(_AEAD_NONCE_SIZE_BYTES)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
        return nonce, ciphertext

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
        if len(key) != AUTHENTICATION_KEY_SIZE_BYTES or len(nonce) != _AEAD_NONCE_SIZE_BYTES:
            _internal_failure(ValueError("aead parameter size"))
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, None)
        except (InvalidTag, ValueError) as cause:
            _internal_failure(cause)
