"""Ed25519 adapters binding the exclusion-policy signing ports to cryptography.

This composition-root module is the only place the pinned
``cryptography==49.0.0`` package meets the framework-neutral signing ports
(spec 13.1): :class:`Ed25519PolicySigner` signs domain-separated messages
with RFC 8032 Ed25519 and derives its ``key_id`` from the raw public key,
while :class:`Ed25519PolicyVerifier` checks signatures under a closed
mapping of already trusted key IDs to raw public keys. Verification failures
are plain ``False`` results — malformed key IDs, wrong signature geometry and
failed verifications never raise and never echo key or message material.

The domain package stays free of any cryptography import; private key
material enters only through the adapter boundary and is never logged,
persisted or serialized by anything in this module. It also imports no
FastAPI or Uvicorn, keeping the shell-only command paths free of server
machinery.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from personal_os.exclusion_policy.errors import (
    KEYSET_KEY_INVALID,
    SIGNING_KEY_ID_INVALID,
    SIGNING_KEY_SIZE_INVALID,
    payload_contract_error,
)
from personal_os.exclusion_policy.signatures import (
    ED25519_PUBLIC_KEY_BYTES,
    ED25519_SEED_BYTES,
    ED25519_SIGNATURE_BYTES,
    PolicySignatureVerifier,
    PolicySigner,
    derive_ed25519_key_id,
    is_wellformed_ed25519_key_id,
)


class Ed25519PolicySigner(PolicySigner):
    """RFC 8032 Ed25519 signer over the closed policy signing domains.

    Constructed from one 32-byte seed (or an already-built
    ``Ed25519PrivateKey`` at the startup boundary); the public key and its
    derived ``key_id`` are computed once so every signature is attributable to
    exactly the announced trust-anchor identifier.
    """

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        public_key_bytes = private_key.public_key().public_bytes_raw()
        self._public_key_bytes = public_key_bytes
        self._key_id = derive_ed25519_key_id(public_key_bytes)

    @classmethod
    def from_seed_bytes(cls, seed_bytes: bytes) -> Ed25519PolicySigner:
        """Build the signer from exactly one raw 32-byte Ed25519 seed."""

        if len(seed_bytes) != ED25519_SEED_BYTES:
            raise payload_contract_error(SIGNING_KEY_SIZE_INVALID)
        return cls(Ed25519PrivateKey.from_private_bytes(seed_bytes))

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def public_key_bytes(self) -> bytes:
        """The raw 32 bytes the derived key identifier was computed from.

        Public material only: the composition root binds the in-transaction
        self-verification verifier to exactly these bytes.
        """

        return self._public_key_bytes

    def sign(self, message: bytes) -> bytes:
        return self._private_key.sign(message)


class Ed25519PolicyVerifier(PolicySignatureVerifier):
    """Fail-closed Ed25519 verifier over a closed trusted-key mapping.

    The mapping is validated once at construction — every key ID must satisfy
    the closed grammar and name exactly 32 raw public-key bytes — and then
    frozen. :meth:`verify` answers ``False`` for unknown key IDs, wrong
    signature geometry and every failed or malformed verification; it never
    raises and never leaks which check failed.
    """

    def __init__(self, public_keys_by_id: Mapping[str, bytes]) -> None:
        validated: dict[str, bytes] = {}
        for key_id, public_key_bytes in public_keys_by_id.items():
            if not is_wellformed_ed25519_key_id(key_id):
                raise payload_contract_error(SIGNING_KEY_ID_INVALID)
            if len(public_key_bytes) != ED25519_PUBLIC_KEY_BYTES:
                raise payload_contract_error(SIGNING_KEY_SIZE_INVALID)
            if key_id in validated:
                raise payload_contract_error(KEYSET_KEY_INVALID)
            validated[key_id] = public_key_bytes
        self._public_keys_by_id: Mapping[str, bytes] = MappingProxyType(validated)

    def verify(self, key_id: str, signature: bytes, message: bytes) -> bool:
        public_key_bytes = self._public_keys_by_id.get(key_id)
        if public_key_bytes is None:
            return False
        if len(signature) != ED25519_SIGNATURE_BYTES:
            return False
        try:
            Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature, message)
        except InvalidSignature, ValueError:
            return False
        return True
