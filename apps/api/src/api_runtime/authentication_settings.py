"""Frozen authentication settings loaded from the approved environment fragment.

This module owns the non-secret Web authentication configuration snapshot: the
exact allowed origin, the trusted-proxy CIDR list, the plugin version bounds
and the versioned authentication key references. Key material itself never
appears in an environment value; only key IDs and exact file names resolved
beneath the shared secret root are configured here, and
:mod:`api_runtime.authentication_crypto` loads the bytes through the
secret-file boundary.

The session and keyring limits exposed as class-level constants are frozen
typed values pinned by the design Global Constraints: pending TOTP five
minutes, idle twelve hours, absolute seven days, recent re-authentication five
minutes and 32-byte master keys with at most four previous keys.

It lives in the API composition-root package: it imports the shared core error
and runtime-configuration contracts and never imports FastAPI or Uvicorn, so
shell-only command paths stay free of server machinery.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from ipaddress import ip_network
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Final, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.environment_names import (
    KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES,
)
from personal_os.runtime_configuration.models import RuntimeEnvironment

_ENVIRONMENT_PREFIX: Final[str] = "KNOWLEDGE_"

#: Pending-TOTP session lifetime: five minutes (Global Constraints).
SESSION_PENDING_TOTP_TTL_SECONDS: Final[int] = 5 * 60

#: Active session idle expiry: twelve hours (Global Constraints).
SESSION_IDLE_TTL_HOURS: Final[int] = 12

#: Active session absolute expiry: seven days (Global Constraints).
SESSION_ABSOLUTE_TTL_DAYS: Final[int] = 7

#: Recent re-authentication window: five minutes (Global Constraints).
RECENT_REAUTHENTICATION_WINDOW_SECONDS: Final[int] = 5 * 60

#: Authentication master-key length: exactly 32 bytes (spec 20.1).
AUTHENTICATION_KEY_SIZE_BYTES: Final[int] = 32

#: Bounded ordered keyring: at most four previous keys beside the current one.
MAXIMUM_PREVIOUS_KEY_COUNT: Final[int] = 4

#: Environments that must not accept a plain-HTTP allowed origin. The explicit
#: loopback local-development mode is reserved for local and test.
_HTTPS_ONLY_ENVIRONMENTS: Final[tuple[RuntimeEnvironment, ...]] = (
    RuntimeEnvironment.STAGING,
    RuntimeEnvironment.PRODUCTION,
)

#: Key file names are forward-slash-joined segments that each start with an
#: alphanumeric character, so ``..`` segments, absolute paths and backslash
#: escapes have no valid spelling in this grammar.
_KEY_FILE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$"
)
_MAXIMUM_KEY_FILE_NAME_LENGTH: Final[int] = 128

#: Plugin version bounds are dotted numeric triples (``1.13.1``).
_PLUGIN_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")

#: Closed map of authentication environment names to model field names. The key
#: set mirrors the core authentication fragment exactly, so the repository-wide
#: registry stays the single source of truth for the approved names.
AUTHENTICATION_ENVIRONMENT_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "KNOWLEDGE_ENVIRONMENT": "environment",
        "KNOWLEDGE_SECRET_ROOT": "secret_root",
        "KNOWLEDGE_AUTH_ALLOWED_ORIGIN": "allowed_origin",
        "KNOWLEDGE_AUTH_TRUSTED_PROXY_CIDRS": "trusted_proxy_cidrs",
        "KNOWLEDGE_AUTH_CURRENT_KEY_ID": "current_key_id",
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "current_key_file",
        "KNOWLEDGE_AUTH_PREVIOUS_KEYS": "previous_key_files",
        "KNOWLEDGE_AUTH_MIN_PLUGIN_VERSION": "minimum_plugin_version",
        "KNOWLEDGE_AUTH_MAX_PLUGIN_VERSION": "maximum_plugin_version",
    }
)


def _validated_key_file_name(file_name: str) -> str:
    """Screen one key file name against the closed relative grammar."""
    if (
        len(file_name) > _MAXIMUM_KEY_FILE_NAME_LENGTH
        or _KEY_FILE_NAME_PATTERN.fullmatch(file_name) is None
    ):
        raise ValueError("key file name must be a relative name beneath the secret root")
    return file_name


def _parse_previous_key_entries(previous_keys: str) -> list[tuple[str, str]]:
    """Parse the bounded comma-separated ``key-id=file-name`` sequence."""
    if previous_keys == "":
        return []
    entries = previous_keys.split(",")
    if len(entries) > MAXIMUM_PREVIOUS_KEY_COUNT:
        raise ValueError("previous keys exceed the allowed count")
    parsed_entries: list[tuple[str, str]] = []
    for entry in entries:
        key_id, separator, file_name = entry.partition("=")
        if not separator or "=" in file_name:
            raise ValueError("previous key entry must be exactly key-id=file-name")
        parsed_entries.append(
            (SafeToken.parse(key_id).value, _validated_key_file_name(file_name)),
        )
    return parsed_entries


def _parse_trusted_proxy_cidrs(trusted_proxy_cidrs: str) -> list[str]:
    """Parse the comma-separated CIDR list, rejecting malformed or host-bit entries."""
    if trusted_proxy_cidrs == "":
        return []
    return [str(ip_network(entry, strict=True)) for entry in trusted_proxy_cidrs.split(",")]


def _require_https_allowed_origin(environment: RuntimeEnvironment, allowed_origin: str) -> None:
    """Reject plain-HTTP origins outside the local-development environments."""
    if environment in _HTTPS_ONLY_ENVIRONMENTS and not allowed_origin.startswith("https://"):
        raise ValueError("allowed origin must use https outside local development")


class AuthenticationSettings(BaseModel):
    """Frozen, validated snapshot of the non-secret authentication configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    SESSION_PENDING_TOTP_TTL_SECONDS: ClassVar[int] = SESSION_PENDING_TOTP_TTL_SECONDS
    SESSION_IDLE_TTL_HOURS: ClassVar[int] = SESSION_IDLE_TTL_HOURS
    SESSION_ABSOLUTE_TTL_DAYS: ClassVar[int] = SESSION_ABSOLUTE_TTL_DAYS
    RECENT_REAUTHENTICATION_WINDOW_SECONDS: ClassVar[int] = RECENT_REAUTHENTICATION_WINDOW_SECONDS
    AUTHENTICATION_KEY_SIZE_BYTES: ClassVar[int] = AUTHENTICATION_KEY_SIZE_BYTES
    MAXIMUM_PREVIOUS_KEY_COUNT: ClassVar[int] = MAXIMUM_PREVIOUS_KEY_COUNT

    environment: RuntimeEnvironment
    secret_root: Path
    allowed_origin: str
    trusted_proxy_cidrs: tuple[str, ...] = ()
    current_key_id: str
    current_key_file: str
    previous_key_files: tuple[tuple[str, str], ...] = ()
    minimum_plugin_version: str
    maximum_plugin_version: str

    @field_validator("secret_root")
    @classmethod
    def _require_absolute_secret_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("secret root must be absolute")
        return value

    @field_validator("allowed_origin")
    @classmethod
    def _require_closed_origin(cls, value: str) -> str:
        """Normalize one exact origin: scheme, host and optional port, nothing else."""
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("allowed origin must not contain whitespace")
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("allowed origin must use the http or https scheme")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("allowed origin must not carry credentials")
        if parsed.path or parsed.query or parsed.fragment:
            raise ValueError("allowed origin must not carry a path, query or fragment")
        host = parsed.hostname
        if not host:
            raise ValueError("allowed origin must name a host")
        try:
            port = parsed.port
        except ValueError as cause:
            raise ValueError("allowed origin port is outside the accepted range") from cause
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("allowed origin port is outside the accepted range")
        rendered_host = f"[{host}]" if ":" in host else host
        if port is None:
            return f"{parsed.scheme}://{rendered_host}"
        return f"{parsed.scheme}://{rendered_host}:{port}"

    @field_validator("trusted_proxy_cidrs", mode="before")
    @classmethod
    def _parse_trusted_proxy_cidr_entries(cls, value: object) -> object:
        if isinstance(value, str):
            return _parse_trusted_proxy_cidrs(value)
        return value

    @field_validator("current_key_id")
    @classmethod
    def _require_safe_key_id(cls, value: str) -> str:
        return SafeToken.parse(value).value

    @field_validator("current_key_file")
    @classmethod
    def _require_relative_key_file(cls, value: str) -> str:
        return _validated_key_file_name(value)

    @field_validator("previous_key_files", mode="before")
    @classmethod
    def _parse_previous_key_file_entries(cls, value: object) -> object:
        if isinstance(value, str):
            return _parse_previous_key_entries(value)
        return value

    @field_validator("minimum_plugin_version", "maximum_plugin_version")
    @classmethod
    def _require_dotted_triple(cls, value: str) -> str:
        if _PLUGIN_VERSION_PATTERN.fullmatch(value) is None:
            raise ValueError("plugin version must be a dotted numeric triple")
        return value

    @model_validator(mode="after")
    def _require_consistent_cross_field_invariants(self) -> Self:
        """Check every rule that spans two or more fields of the snapshot."""
        minimum_bound = tuple(int(part) for part in self.minimum_plugin_version.split("."))
        maximum_bound = tuple(int(part) for part in self.maximum_plugin_version.split("."))
        if minimum_bound > maximum_bound:
            raise ValueError("minimum plugin version must not exceed the maximum")

        _require_https_allowed_origin(self.environment, self.allowed_origin)

        previous_key_ids = [key_id for key_id, _file_name in self.previous_key_files]
        previous_file_names = [file_name for _key_id, file_name in self.previous_key_files]
        if len(set(previous_key_ids)) != len(previous_key_ids) or len(
            set(previous_file_names)
        ) != len(previous_file_names):
            raise ValueError("previous key references must be unique")
        if self.current_key_id in previous_key_ids:
            raise ValueError("previous key ids must not collide with the current key id")
        if self.current_key_file in previous_file_names:
            raise ValueError("previous key files must not collide with the current key file")
        return self


def load_authentication_settings(
    *,
    environ: Mapping[str, str] | None = None,
) -> AuthenticationSettings:
    """Load a frozen :class:`AuthenticationSettings` from an environment snapshot.

    ``environ`` defaults to ``os.environ`` read at call time (never at import
    time). Any ``KNOWLEDGE_*`` key outside the repository-wide known-name
    registry raises :class:`ConfigurationError` without echoing the offending
    name or value; registered names owned by another fragment (runtime,
    database, object storage, temporal) are ignored. Grammar failures surface
    as ``configuration_invalid`` with registered field names only; the origin,
    CIDR and key-file values themselves never enter an error detail.
    """
    source = dict(os.environ if environ is None else environ)
    unknown_count = sum(
        1
        for key in source
        if key.startswith(_ENVIRONMENT_PREFIX) and key not in KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES
    )
    if unknown_count:
        raise ConfigurationError(
            ErrorCode.CONFIGURATION_UNKNOWN_KEY,
            safe_details={"count": unknown_count},
        )
    values = {
        field_name: source[environment_name]
        for environment_name, field_name in AUTHENTICATION_ENVIRONMENT_FIELDS.items()
        if environment_name in source
    }
    try:
        # Pydantic validates the heterogeneous env values at runtime; mypy cannot
        # statically prove the dynamic dict maps onto the typed model fields.
        return AuthenticationSettings(**values)  # type: ignore[arg-type]
    except ValidationError as cause:
        field_names = tuple(
            SafeToken.parse(str(error["loc"][0]))
            for error in cause.errors(include_input=False, include_url=False)
            if error["loc"]
        )
        raise ConfigurationError(
            ErrorCode.CONFIGURATION_INVALID,
            safe_details={"count": len(cause.errors()), "field_names": field_names},
        ) from cause
