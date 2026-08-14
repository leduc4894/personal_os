"""Frozen database migration settings and the leak-safe URL boundary.

This module owns the Alembic connection contract for the canonical PostgreSQL
baseline. It composes the existing secret-file safety rules into a frozen
:class:`DatabaseMigrationSettings` snapshot, reads the bounded password file,
and builds a SQLAlchemy :class:`~sqlalchemy.engine.URL` plus psycopg connect
arguments without ever rendering the URL or echoing a credential.

SQLAlchemy and psycopg are approved migration-only dependencies. They are
imported here deliberately and must never be imported from ``src/personal_os/``.
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
from sqlalchemy.engine import URL

# Prime a pre-existing circular import inside ``personal_os``: importing
# ``error_contracts.exceptions`` first would re-enter it before
# ``DiagnosticContextError`` is defined. Importing the diagnostics package first
# (``events`` is self-contained) resolves the cycle deterministically for every
# importer of this module, including the future Alembic ``env.py`` entry point.
import personal_os.diagnostics.events  # noqa: F401
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import DatabaseMigrationError
from personal_os.runtime_configuration.environment_names import (
    KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES,
)
from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.runtime_configuration.secret_files import read_secret_file

_ENVIRONMENT_PREFIX: Final[str] = "KNOWLEDGE_"
_DATABASE_DRIVER: Final[str] = "postgresql+psycopg"
_APPLICATION_NAME: Final[str] = "knowledge-migration"
_CONNECT_TIMEOUT_SECONDS: Final[int] = 5
_POSTGRES_OPTIONS: Final[str] = (
    "-c timezone=UTC -c lock_timeout=5000 "
    "-c statement_timeout=60000 "
    "-c idle_in_transaction_session_timeout=60000"
)

# Sensible upper bounds for the safe connection fields. The canonical spec does
# not pin exact lengths for these settings, so the values track the relevant
# PostgreSQL/host boundaries: 253 characters for an FQDN and the default
# ``NAMEDATALEN - 1`` (63) identifier length for the database name and role.
_MAXIMUM_HOST_LENGTH: Final[int] = 253
_MAXIMUM_IDENTIFIER_LENGTH: Final[int] = 63
_MAXIMUM_PASSWORD_FILE_NAME_LENGTH: Final[int] = 255

DATABASE_ENVIRONMENT_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
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
    """Closed set of PostgreSQL sslmode values accepted by the migration."""

    DISABLE = "disable"
    VERIFY_FULL = "verify-full"


def _require_safe_connection_field(value: str, *, maximum_length: int) -> str:
    """Trim, bound and screen a host/database/user connection field.

    Leading/trailing whitespace is trimmed; the trimmed value must be non-empty,
    within ``maximum_length`` and free of whitespace and control characters so a
    sentinel value can never be smuggled into a connection string.
    """
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("connection field must not be empty")
    if len(trimmed) > maximum_length:
        raise ValueError("connection field exceeds the accepted length")
    if any(character.isspace() or not character.isprintable() for character in trimmed):
        raise ValueError("connection field contains an unsupported character")
    return trimmed


class DatabaseMigrationSettings(BaseModel):
    """Frozen, validated snapshot of the canonical migration connection."""

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
    def _require_single_relative_file_name(cls, value: str) -> str:
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

    @model_validator(mode="after")
    def _enforce_ssl_environment_pairing(self) -> DatabaseMigrationSettings:
        non_secure_environments = (RuntimeEnvironment.LOCAL, RuntimeEnvironment.TEST)
        if self.environment in non_secure_environments:
            if self.ssl_mode is not DatabaseSslMode.DISABLE:
                raise ValueError("ssl mode is not accepted for this environment")
        elif self.ssl_mode is not DatabaseSslMode.VERIFY_FULL:
            raise ValueError("ssl mode is not accepted for this environment")
        return self

    def __repr__(self) -> str:
        return "DatabaseMigrationSettings(redacted)"

    def __str__(self) -> str:
        return "DatabaseMigrationSettings(redacted)"


def load_database_migration_settings(
    *,
    environ: Mapping[str, str] | None = None,
) -> DatabaseMigrationSettings:
    """Load a frozen :class:`DatabaseMigrationSettings` from an environment snapshot.

    ``environ`` defaults to ``os.environ`` read at call time (never at import
    time). Any ``KNOWLEDGE_*`` key outside the repository-wide known-name
    registry, and every Pydantic validation failure, maps to a
    :class:`DatabaseMigrationError` with no echoed key, value or input.
    Registered names owned by another fragment (runtime, object storage) are
    ignored; only this fragment's own field map is parsed.
    """
    source = dict(os.environ if environ is None else environ)
    unknown_count = sum(
        1
        for key in source
        if key.startswith(_ENVIRONMENT_PREFIX) and key not in KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES
    )
    if unknown_count:
        raise DatabaseMigrationError(ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID)
    values = {
        field_name: source[environment_name]
        for environment_name, field_name in DATABASE_ENVIRONMENT_FIELDS.items()
        if environment_name in source
    }
    try:
        # Pydantic validates the heterogeneous env values at runtime; mypy cannot
        # statically prove the dynamic dict maps onto the typed model fields.
        return DatabaseMigrationSettings(**values)  # type: ignore[arg-type]
    except ValidationError:
        raise DatabaseMigrationError(ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID) from None


def read_database_password(settings: DatabaseMigrationSettings) -> SecretStr:
    """Read the bounded password file beneath the configured secret root.

    The secret root must already be absolute; this is re-checked here as
    defense in depth before delegating to :func:`read_secret_file`.
    :class:`SecretFileError` is never caught: the existing missing, out-of-root,
    insecure-permission and unreadable contract propagates unchanged.
    """
    if not settings.secret_root.is_absolute():
        raise DatabaseMigrationError(ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID)
    return read_secret_file(
        settings.secret_root / settings.password_file_name,
        settings.secret_root,
    )


def build_database_url(
    settings: DatabaseMigrationSettings,
    password: SecretStr,
) -> URL:
    """Build the SQLAlchemy migration URL without ever rendering it.

    The password is placed into the :class:`URL` value only; the URL is never
    converted to a string, so diagnostics can never carry the credential.
    """
    return URL.create(
        drivername=_DATABASE_DRIVER,
        username=settings.database_user,
        password=password.get_secret_value(),
        host=settings.host,
        port=settings.port,
        database=settings.database_name,
    )


def build_database_connect_arguments(
    settings: DatabaseMigrationSettings,
) -> Mapping[str, str | int]:
    """Build the fixed psycopg connect arguments for the migration connection."""
    return {
        "connect_timeout": _CONNECT_TIMEOUT_SECONDS,
        "sslmode": settings.ssl_mode.value,
        "application_name": _APPLICATION_NAME,
        "options": _POSTGRES_OPTIONS,
    }
