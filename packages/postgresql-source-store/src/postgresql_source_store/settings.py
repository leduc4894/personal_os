"""Frozen source-database runtime settings and the secret-file password boundary.

This module owns the runtime database configuration snapshot for the source
store adapter. It composes the shared runtime-configuration environment-name
registry and the bounded secret-file loader into a frozen
:class:`DatabaseRuntimeSettings` value plus the secret-file-only password
read. There is no ``DATABASE_URL``, no plaintext password environment
variable, no ``.env`` and no ambient libpq fallback; the only inputs are the
passed environment mapping and the bounded password file resolved beneath
``KNOWLEDGE_SECRET_ROOT``. The pinned pool and timeout bounds live here as
constants because they are fixed runtime limits, not configuration.

It lives in the provider package: it imports the shared core error, settings
and secret-file contracts, but the core package never imports it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
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
from personal_os.runtime_configuration.secret_files import read_secret_file

_ENVIRONMENT_PREFIX: Final[str] = "KNOWLEDGE_"

# The URL-style connection string is deliberately not an accepted input: a
# single escaped DSN would smuggle credentials and arbitrary connection
# parameters past the closed field grammar below.
_REJECTED_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset({"DATABASE_URL"})

# Pinned runtime bounds from the canonical transaction contract: pool of 4
# connections with 4 overflow per process, a 5-second pool checkout timeout, a
# 5-second connect timeout and local transaction timeouts of 5/15/30 seconds.
POOL_SIZE: Final[int] = 4
MAX_POOL_OVERFLOW: Final[int] = 4
POOL_TIMEOUT_SECONDS: Final[int] = 5
CONNECT_TIMEOUT_SECONDS: Final[int] = 5
LOCK_TIMEOUT_SECONDS: Final[int] = 5
STATEMENT_TIMEOUT_SECONDS: Final[int] = 15
IDLE_IN_TRANSACTION_SESSION_TIMEOUT_SECONDS: Final[int] = 30

# Sensible upper bounds for the safe connection fields, tracking the relevant
# PostgreSQL/host boundaries: 253 characters for an FQDN and the default
# ``NAMEDATALEN - 1`` (63) identifier length for the database name and role.
_MAXIMUM_HOST_LENGTH: Final[int] = 253
_MAXIMUM_IDENTIFIER_LENGTH: Final[int] = 63
_MAXIMUM_PASSWORD_FILE_NAME_LENGTH: Final[int] = 255

#: Closed map of database environment names to model field names. The key set
#: mirrors the core database fragment exactly, so the repository-wide registry
#: stays the single source of truth for the approved names.
DATABASE_RUNTIME_ENVIRONMENT_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "KNOWLEDGE_ENVIRONMENT": "environment",
        "KNOWLEDGE_SECRET_ROOT": "secret_root",
        "KNOWLEDGE_DATABASE_HOST": "host",
        "KNOWLEDGE_DATABASE_PORT": "port",
        "KNOWLEDGE_DATABASE_NAME": "database_name",
        "KNOWLEDGE_DATABASE_USER": "database_user",
        "KNOWLEDGE_DATABASE_PASSWORD_FILE": "password_file_name",
        "KNOWLEDGE_DATABASE_SSL_MODE": "ssl_mode",
    }
)


class DatabaseSslMode(StrEnum):
    """Closed set of PostgreSQL sslmode values accepted by the source store."""

    DISABLE = "disable"
    VERIFY_FULL = "verify-full"


def _require_safe_connection_field(value: str, *, maximum_length: int) -> str:
    """Trim, bound and screen a host/database/user connection field.

    Leading/trailing whitespace is trimmed; the trimmed value must be
    non-empty, within ``maximum_length`` and free of whitespace and control
    characters so a sentinel value can never be smuggled into a connection
    string.
    """
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("connection field must not be empty")
    if len(trimmed) > maximum_length:
        raise ValueError("connection field exceeds the accepted length")
    if any(character.isspace() or not character.isprintable() for character in trimmed):
        raise ValueError("connection field contains an unsupported character")
    return trimmed


def _require_single_relative_file_name(value: str) -> str:
    """Reject any password file name that is not a single relative component."""
    if not value:
        raise ValueError("password file name must not be empty")
    if "\x00" in value:
        raise ValueError("password file name must not contain NUL")
    if value in (".", ".."):
        raise ValueError("password file name must be a regular file name")
    if "/" in value or "\\" in value:
        raise ValueError("password file name must be a single component")
    if len(value) > _MAXIMUM_PASSWORD_FILE_NAME_LENGTH:
        raise ValueError("password file name exceeds the accepted length")
    return value


class DatabaseRuntimeSettings(BaseModel):
    """Frozen, validated snapshot of the runtime source-database connection.

    The snapshot carries only non-secret configuration. The password never
    enters this model; it is read into a short-lived :class:`SecretStr` by
    :func:`read_database_runtime_password` only while constructing an engine.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    environment: RuntimeEnvironment = RuntimeEnvironment.LOCAL
    secret_root: Path = Path("/run/secrets")
    host: str = "127.0.0.1"
    port: int = 5432
    database_name: str = "knowledge"
    database_user: str = "knowledge_app"
    password_file_name: str = "postgres_application_password"
    ssl_mode: DatabaseSslMode = DatabaseSslMode.DISABLE

    @field_validator("secret_root")
    @classmethod
    def _require_absolute_secret_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("secret root must be absolute")
        return value

    @field_validator("host")
    @classmethod
    def _require_safe_host(cls, value: str) -> str:
        return _require_safe_connection_field(value, maximum_length=_MAXIMUM_HOST_LENGTH)

    @field_validator("database_name")
    @classmethod
    def _require_safe_database_name(cls, value: str) -> str:
        return _require_safe_connection_field(value, maximum_length=_MAXIMUM_IDENTIFIER_LENGTH)

    @field_validator("database_user")
    @classmethod
    def _require_safe_database_user(cls, value: str) -> str:
        return _require_safe_connection_field(value, maximum_length=_MAXIMUM_IDENTIFIER_LENGTH)

    @field_validator("port")
    @classmethod
    def _require_port_in_bind_range(cls, value: int) -> int:
        if value < 1 or value > 65535:
            raise ValueError("port must be within the PostgreSQL bind range")
        return value

    @field_validator("password_file_name")
    @classmethod
    def _require_password_file_name(cls, value: str) -> str:
        return _require_single_relative_file_name(value)

    @model_validator(mode="after")
    def _enforce_ssl_environment_pairing(self) -> DatabaseRuntimeSettings:
        non_secure_environments = (RuntimeEnvironment.LOCAL, RuntimeEnvironment.TEST)
        if self.environment in non_secure_environments:
            if self.ssl_mode is not DatabaseSslMode.DISABLE:
                raise ValueError("ssl mode is not accepted for this environment")
        elif self.ssl_mode is not DatabaseSslMode.VERIFY_FULL:
            raise ValueError("ssl mode is not accepted for this environment")
        return self

    def __repr__(self) -> str:
        return "DatabaseRuntimeSettings(redacted)"

    def __str__(self) -> str:
        return "DatabaseRuntimeSettings(redacted)"


def load_database_runtime_settings(
    *,
    environ: Mapping[str, str] | None = None,
) -> DatabaseRuntimeSettings:
    """Load a frozen :class:`DatabaseRuntimeSettings` from an environment snapshot.

    ``environ`` defaults to ``os.environ`` read at call time (never at import
    time). ``DATABASE_URL`` and any ``KNOWLEDGE_*`` key outside the
    repository-wide known-name registry (including the plaintext
    ``KNOWLEDGE_DATABASE_PASSWORD``) raise :class:`ConfigurationError` without
    echoing the offending name or value; registered names owned by another
    fragment (runtime, object storage, temporal) are ignored. Every Pydantic
    validation failure maps to ``configuration_invalid`` with registered field
    names only.
    """
    source = dict(os.environ if environ is None else environ)
    rejected_count = sum(1 for name in _REJECTED_ENVIRONMENT_NAMES if name in source)
    unknown_count = sum(
        1
        for key in source
        if key.startswith(_ENVIRONMENT_PREFIX) and key not in KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES
    )
    if rejected_count or unknown_count:
        raise ConfigurationError(
            ErrorCode.CONFIGURATION_UNKNOWN_KEY,
            safe_details={"count": rejected_count + unknown_count},
        )
    values = {
        field_name: source[environment_name]
        for environment_name, field_name in DATABASE_RUNTIME_ENVIRONMENT_FIELDS.items()
        if environment_name in source
    }
    try:
        # Pydantic validates the heterogeneous env values at runtime; mypy cannot
        # statically prove the dynamic dict maps onto the typed model fields.
        return DatabaseRuntimeSettings(**values)  # type: ignore[arg-type]
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


def read_database_runtime_password(settings: DatabaseRuntimeSettings) -> SecretStr:
    """Read the bounded password file beneath the configured secret root.

    The secret root must already be absolute; this is re-checked here as
    defense in depth before delegating to :func:`read_secret_file`.
    :class:`SecretFileError` is never caught: the existing missing, out-of-root,
    insecure-permission and unreadable contract propagates unchanged.
    """
    if not settings.secret_root.is_absolute():
        raise ConfigurationError(ErrorCode.CONFIGURATION_INVALID)
    return read_secret_file(
        settings.secret_root / settings.password_file_name,
        settings.secret_root,
    )
