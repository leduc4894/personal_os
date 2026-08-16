"""Provider-neutral ports for password hashing and authentication crypto.

The domain owns the contracts; the reviewed concrete adapters live in the API
composition root (:mod:`api_runtime.authentication_crypto`) and bind
``argon2-cffi`` and ``cryptography`` there, so this package stays importable
without either dependency. A deterministic test or offline-export double can
implement these protocols without any native library.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PasswordHasherPort(Protocol):
    """Argon2id password hashing seam (spec 8.1).

    ``hash_password`` returns the encoded PHC string carrying its algorithm
    and work parameters; ``needs_rehash`` reports whether a stored PHC string
    predates the pinned parameters so a successful login can upgrade it.
    """

    def hash_password(self, password: str) -> str: ...

    def verify_password(self, password_hash: str, password: str) -> bool: ...

    def needs_rehash(self, password_hash: str) -> bool: ...


@runtime_checkable
class AuthenticationCryptoPort(Protocol):
    """AEAD/HKDF/HMAC seam over the versioned master keyring (spec 20.1).

    ``derive_subkey`` applies RFC 5869 HKDF-SHA-256 with a 32-byte zero salt
    and one explicit domain ``label``; ``seal_secret`` applies AES-256-GCM
    with a fresh 12-byte nonce and returns ``(nonce, ciphertext)``;
    ``open_secret`` decrypts and fails closed on any tag or parameter
    failure. Every caller picks the domain label from
    :mod:`personal_os.authentication.crypto` so subkeys never mix domains.
    """

    def derive_subkey(self, *, master_key: bytes, label: str) -> bytes: ...

    def hmac_sha256(self, *, key: bytes, message: bytes) -> bytes: ...

    def seal_secret(self, *, key: bytes, plaintext: bytes) -> tuple[bytes, bytes]: ...

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes: ...
