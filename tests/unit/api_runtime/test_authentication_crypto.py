"""Fail-before-bind authentication keyring loading from exact secret files.

These tests prove the keyring loader: every referenced key file is read
through the existing secret-file boundary, each decoded key must be exactly 32
bytes of valid hexadecimal key material, malformed or wrongly sized material
fails with ``configuration_secret_invalid``, a boundary failure (missing file)
propagates its own registered code, and the returned keyring exposes only
immutable mappings.

They also prove the concrete Argon2id password-hashing and AEAD/HKDF/HMAC
adapters that implement the framework-neutral authentication ports: the
pinned work parameters round-trip through the PHC string, weak parameter sets
demand a rehash, malformed stored hashes and AEAD failures fail closed as the
safe ``internal_error`` without echoing crypto text, and the HKDF/HMAC
composition reproduces the reviewed golden derivation vector.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import argon2
import pytest
from api_runtime.authentication_composition import OfflineAuthenticationCrypto
from api_runtime.authentication_crypto import (
    Argon2PasswordHasher,
    AuthenticationKeyring,
    CryptographyAuthenticationCrypto,
    load_authentication_keyring,
)
from api_runtime.authentication_settings import (
    AuthenticationSettings,
    load_authentication_settings,
)

from personal_os.authentication.crypto import (
    CRYPTO_DOMAIN_LABELS,
    CSRF_HASH_LABEL,
    REFRESH_REPLAY_DERIVATION_LABEL,
    THROTTLE_HMAC_LABEL,
)
from personal_os.authentication.passwords import (
    ARGON2ID_MEMORY_COST_KIB,
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
    SecretFileError,
)

CURRENT_KEY_BYTES: bytes = bytes(range(32))
CURRENT_KEY_HEX: str = CURRENT_KEY_BYTES.hex()
PREVIOUS_KEY_BYTES: bytes = bytes(range(32, 64))
PREVIOUS_KEY_HEX: str = PREVIOUS_KEY_BYTES.hex()


def authentication_settings(
    secret_root: Path,
    *,
    previous_keys: str = "",
) -> AuthenticationSettings:
    """Build a valid authentication snapshot bound to ``secret_root``."""
    environ: dict[str, str] = {
        "KNOWLEDGE_ENVIRONMENT": "test",
        "KNOWLEDGE_SECRET_ROOT": str(secret_root),
        "KNOWLEDGE_AUTH_ALLOWED_ORIGIN": "https://admin.example.test",
        "KNOWLEDGE_AUTH_CURRENT_KEY_ID": "auth-key-1",
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "auth-current.key",
        "KNOWLEDGE_AUTH_PREVIOUS_KEYS": previous_keys,
        "KNOWLEDGE_AUTH_MIN_PLUGIN_VERSION": "1.13.0",
        "KNOWLEDGE_AUTH_MAX_PLUGIN_VERSION": "1.13.1",
    }
    return load_authentication_settings(environ=environ)


def test_authentication_keyring_rejects_short_current_key(tmp_path: Path) -> None:
    (tmp_path / "auth-current.key").write_bytes(b"short")
    settings = authentication_settings(secret_root=tmp_path)
    with pytest.raises(ConfigurationError) as raised:
        load_authentication_keyring(settings)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_SECRET_INVALID


@pytest.mark.parametrize(
    ("key_material", "reason"),
    [
        ("00" * 31, "wrong_length"),
        ("00" * 33, "wrong_length"),
        ("z" * 64, "invalid_encoding"),
        ("0" * 63, "invalid_encoding"),
    ],
)
def test_key_material_must_be_exactly_32_hexadecimal_bytes(
    tmp_path: Path,
    key_material: str,
    reason: str,
) -> None:
    (tmp_path / "auth-current.key").write_bytes(key_material.encode("ascii"))
    settings = authentication_settings(secret_root=tmp_path)
    with pytest.raises(ConfigurationError) as raised:
        load_authentication_keyring(settings)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_SECRET_INVALID
    assert raised.value.safe_details["reason"] == SafeToken.parse(reason)


def test_keyring_loads_current_and_previous_keys(tmp_path: Path) -> None:
    (tmp_path / "auth-current.key").write_text(CURRENT_KEY_HEX, encoding="ascii")
    (tmp_path / "auth-0.key").write_text(PREVIOUS_KEY_HEX, encoding="ascii")
    settings = authentication_settings(
        secret_root=tmp_path,
        previous_keys="auth-key-0=auth-0.key",
    )
    keyring = load_authentication_keyring(settings)
    assert isinstance(keyring, AuthenticationKeyring)
    assert keyring.current_key_id == "auth-key-1"
    assert keyring.current_key() == CURRENT_KEY_BYTES
    assert dict(keyring.keys_by_id) == {
        "auth-key-1": CURRENT_KEY_BYTES,
        "auth-key-0": PREVIOUS_KEY_BYTES,
    }


def test_keyring_trailing_newline_is_tolerated(tmp_path: Path) -> None:
    (tmp_path / "auth-current.key").write_bytes(
        f"{CURRENT_KEY_HEX}\r\n".encode("ascii"),
    )
    settings = authentication_settings(secret_root=tmp_path)
    keyring = load_authentication_keyring(settings)
    assert keyring.current_key() == CURRENT_KEY_BYTES


def test_keyring_mappings_are_immutable(tmp_path: Path) -> None:
    (tmp_path / "auth-current.key").write_text(CURRENT_KEY_HEX, encoding="ascii")
    settings = authentication_settings(secret_root=tmp_path)
    keyring = load_authentication_keyring(settings)
    with pytest.raises(TypeError):
        keyring.keys_by_id["intruder"] = b"0" * 32


def test_missing_key_file_fails_through_the_secret_file_boundary(
    tmp_path: Path,
) -> None:
    settings = authentication_settings(secret_root=tmp_path)
    with pytest.raises(SecretFileError) as raised:
        load_authentication_keyring(settings)
    assert raised.value.error_code is ErrorCode.SECRET_FILE_MISSING


def test_malformed_previous_key_material_fails_before_keyring_return(
    tmp_path: Path,
) -> None:
    (tmp_path / "auth-current.key").write_text(CURRENT_KEY_HEX, encoding="ascii")
    (tmp_path / "auth-0.key").write_text("short", encoding="ascii")
    settings = authentication_settings(
        secret_root=tmp_path,
        previous_keys="auth-key-0=auth-0.key",
    )
    with pytest.raises(ConfigurationError) as raised:
        load_authentication_keyring(settings)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_SECRET_INVALID


# --- Argon2id password-hashing adapter ---------------------------------------


def test_argon2_adapter_implements_the_password_hasher_port() -> None:
    hasher = Argon2PasswordHasher()
    assert isinstance(hasher, PasswordHasherPort)


def test_argon2_adapter_hashes_with_pinned_argon2id_parameters() -> None:
    password_hash = Argon2PasswordHasher().hash_password("correct horse battery staple!")
    assert password_hash.startswith(
        f"$argon2id$v=19$m={ARGON2ID_MEMORY_COST_KIB},t={ARGON2ID_TIME_COST_ITERATIONS},p=1$"
    )


def test_argon2_adapter_verifies_and_rejects_passwords() -> None:
    hasher = Argon2PasswordHasher()
    password_hash = hasher.hash_password("a unique passphrase value")
    assert hasher.verify_password(password_hash, "a unique passphrase value") is True
    assert hasher.verify_password(password_hash, "another passphrase value") is False


def test_argon2_adapter_demands_rehash_only_for_obsolete_parameters() -> None:
    hasher = Argon2PasswordHasher()
    assert hasher.needs_rehash(hasher.hash_password("a unique passphrase value")) is False
    obsolete_hasher = argon2.PasswordHasher(
        type=argon2.Type.ID,
        memory_cost=19456,
        time_cost=2,
        parallelism=1,
        salt_len=16,
        hash_len=32,
    )
    assert hasher.needs_rehash(obsolete_hasher.hash("a unique passphrase value")) is True


def test_argon2_adapter_fails_closed_on_malformed_stored_hash() -> None:
    hasher = Argon2PasswordHasher()
    with pytest.raises(InternalApplicationError) as raised:
        hasher.verify_password("not-a-phc-string", "a unique passphrase value")
    assert raised.value.error_code is ErrorCode.INTERNAL_ERROR
    rendered = f"{raised.value!r} {raised.value}"
    assert "not-a-phc-string" not in rendered


# --- AEAD/HKDF/HMAC crypto adapter -------------------------------------------


def test_crypto_adapter_implements_the_authentication_crypto_port() -> None:
    assert isinstance(CryptographyAuthenticationCrypto(), AuthenticationCryptoPort)


def test_hmac_sha256_matches_rfc_4231_test_case_one() -> None:
    digest = CryptographyAuthenticationCrypto().hmac_sha256(
        key=bytes([0x0B] * 20),
        message=b"Hi There",
    )
    assert digest.hex() == "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"


def test_refresh_replay_derivation_reproduces_the_reviewed_golden_vector() -> None:
    crypto = CryptographyAuthenticationCrypto()
    prf_key = crypto.derive_subkey(
        master_key=bytes(range(32)),
        label=REFRESH_REPLAY_DERIVATION_LABEL,
    )
    successor_secret = crypto.hmac_sha256(
        key=prf_key,
        message=(
            bytes(range(32, 64))
            + UUID("00000000-0000-0000-0000-000000000001").bytes
            + UUID("00000000-0000-0000-0000-000000000002").bytes
            + (2).to_bytes(8, "big")
        ),
    )
    assert successor_secret.hex() == (
        "266ad59acb65e0a437eb79891fa1a349fd1a5e90f531ccf3e442b1920d8a5141"
    )


def test_domain_separated_subkeys_differ_per_label() -> None:
    crypto = CryptographyAuthenticationCrypto()
    csrf_key = crypto.derive_subkey(master_key=CURRENT_KEY_BYTES, label=CSRF_HASH_LABEL)
    throttle_key = crypto.derive_subkey(master_key=CURRENT_KEY_BYTES, label=THROTTLE_HMAC_LABEL)
    assert csrf_key != throttle_key
    assert len(csrf_key) == 32
    assert csrf_key == crypto.derive_subkey(master_key=CURRENT_KEY_BYTES, label=CSRF_HASH_LABEL)


def test_seal_and_open_secret_roundtrip_with_fresh_twelve_byte_nonces() -> None:
    crypto = CryptographyAuthenticationCrypto()
    first_nonce, first_ciphertext = crypto.seal_secret(
        key=CURRENT_KEY_BYTES, plaintext=PREVIOUS_KEY_BYTES
    )
    second_nonce, second_ciphertext = crypto.seal_secret(
        key=CURRENT_KEY_BYTES, plaintext=PREVIOUS_KEY_BYTES
    )
    assert len(first_nonce) == 12
    assert first_nonce != second_nonce
    assert first_ciphertext != second_ciphertext
    opened = crypto.open_secret(
        key=CURRENT_KEY_BYTES, nonce=first_nonce, ciphertext=first_ciphertext
    )
    assert opened == PREVIOUS_KEY_BYTES


def test_open_secret_fails_closed_without_leaking_crypto_text() -> None:
    crypto = CryptographyAuthenticationCrypto()
    nonce, ciphertext = crypto.seal_secret(key=CURRENT_KEY_BYTES, plaintext=b"secret payload")
    with pytest.raises(InternalApplicationError) as raised:
        crypto.open_secret(key=PREVIOUS_KEY_BYTES, nonce=nonce, ciphertext=ciphertext)
    assert raised.value.error_code is ErrorCode.INTERNAL_ERROR
    rendered = f"{raised.value!r} {raised.value}"
    assert "secret payload" not in rendered


def test_derive_subkey_rejects_non_ascii_labels_and_wrong_key_sizes() -> None:
    crypto = CryptographyAuthenticationCrypto()
    with pytest.raises(InternalApplicationError):
        crypto.derive_subkey(master_key=CURRENT_KEY_BYTES, label="auth/ütf/v1")
    with pytest.raises(InternalApplicationError):
        crypto.derive_subkey(master_key=b"short", label=CSRF_HASH_LABEL)


def test_derive_subkey_rejects_label_outside_crypto_domain_vocabulary() -> None:
    """A label not in CRYPTO_DOMAIN_LABELS is a contract violation.

    The closed vocabulary (spec 20.1) prevents subkey domain confusion. A label
    that passes ASCII + length checks but is not in the vocabulary must fail
    closed as the safe ``internal_error`` without echoing the rejected label.
    """
    crypto = CryptographyAuthenticationCrypto()
    with pytest.raises(InternalApplicationError) as raised:
        crypto.derive_subkey(
            master_key=CURRENT_KEY_BYTES,
            label="auth/not-a-real-domain/v1",
        )
    assert raised.value.error_code is ErrorCode.INTERNAL_ERROR
    rendered = f"{raised.value!r} {raised.value}"
    assert "auth/not-a-real-domain/v1" not in rendered


def test_derive_subkey_accepts_every_registered_domain_label() -> None:
    """Every label in CRYPTO_DOMAIN_LABELS must derive successfully.

    This locks the membership set: any future addition to the vocabulary must
    come with an updated test so the contract stays closed.
    """
    crypto = CryptographyAuthenticationCrypto()
    for label in CRYPTO_DOMAIN_LABELS:
        subkey = crypto.derive_subkey(master_key=CURRENT_KEY_BYTES, label=label)
        assert len(subkey) == 32, f"subkey for {label!r} must be 32 bytes"


def test_offline_derive_subkey_rejects_label_outside_crypto_domain_vocabulary() -> None:
    """Offline crypto mirrors the production membership check.

    The offline composition is a deterministic double. It must reject
    out-of-vocabulary labels so any test wiring that bypasses the production
    adapter still cannot mix subkey domains.
    """
    crypto = OfflineAuthenticationCrypto()
    with pytest.raises(InternalApplicationError) as raised:
        crypto.derive_subkey(
            master_key=CURRENT_KEY_BYTES,
            label="auth/not-a-real-domain/v1",
        )
    assert raised.value.error_code is ErrorCode.INTERNAL_ERROR
    rendered = f"{raised.value!r} {raised.value}"
    assert "auth/not-a-real-domain/v1" not in rendered


def test_offline_derive_subkey_accepts_every_registered_domain_label() -> None:
    """Offline crypto accepts every label in CRYPTO_DOMAIN_LABELS."""
    crypto = OfflineAuthenticationCrypto()
    for label in CRYPTO_DOMAIN_LABELS:
        subkey = crypto.derive_subkey(master_key=CURRENT_KEY_BYTES, label=label)
        assert len(subkey) == 32, f"subkey for {label!r} must be 32 bytes"
