"""Fail-before-bind authentication keyring loading from exact secret files.

These tests prove the keyring loader: every referenced key file is read
through the existing secret-file boundary, each decoded key must be exactly 32
bytes of valid hexadecimal key material, malformed or wrongly sized material
fails with ``configuration_secret_invalid``, a boundary failure (missing file)
propagates its own registered code, and the returned keyring exposes only
immutable mappings.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from api_runtime.authentication_crypto import (
    AuthenticationKeyring,
    load_authentication_keyring,
)
from api_runtime.authentication_settings import (
    AuthenticationSettings,
    load_authentication_settings,
)

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError, SecretFileError

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
