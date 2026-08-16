"""Fail-before-bind authentication keyring loading from exact secret files.

This module loads the bounded ordered keyring of versioned 32-byte
authentication master keys (spec 20.1). Every key file named by the
authentication settings is read through the existing secret-file boundary, so
missing, out-of-root, wrong-type, oversized or permission-unsafe files keep
their registered ``secret_file_*`` codes and refuse startup before socket
exposure. Key material must be exactly 64 hexadecimal characters (32 bytes);
anything else fails as ``configuration_secret_invalid`` without echoing the
file name or material.

It lives in the API composition-root package and never imports FastAPI or
Uvicorn, so the shell-only command paths stay free of server machinery and
never read a key file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from pydantic import SecretStr

from api_runtime.authentication_settings import (
    AUTHENTICATION_KEY_SIZE_BYTES,
    AuthenticationSettings,
)
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.secret_files import read_secret_file

#: Key material grammar: hexadecimal characters only. The separate length check
#: pins the decoded size to exactly 32 bytes.
_KEY_MATERIAL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]+$")


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
