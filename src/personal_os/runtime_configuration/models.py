"""Frozen runtime settings model and its bounded value enums."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class ServiceName(StrEnum):
    API = "api"
    MCP = "mcp"
    WORKER = "worker"


class RuntimeEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ConfiguredLogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class RuntimeSettings(BaseSettings):
    """Immutable, validated view of the runtime configuration snapshot.

    Pydantic's own environment, dotenv and file-secret sources are disabled via
    :meth:`settings_customise_sources`; :mod:`loading` is the single owner of the
    environment snapshot and the unknown-key check.
    """

    model_config = SettingsConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        env_file=None,
        enable_decoding=False,
    )

    service_name: ServiceName
    environment: RuntimeEnvironment = RuntimeEnvironment.LOCAL
    log_level: ConfiguredLogLevel = ConfiguredLogLevel.INFO
    secret_root: Path = Path("/run/secrets")
    diagnostics_log_dir: Path | None = None

    @field_validator("secret_root")
    @classmethod
    def require_absolute_secret_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("secret root must be absolute")
        return value

    @field_validator("diagnostics_log_dir", mode="before")
    @classmethod
    def blank_diagnostics_log_dir_means_disabled(cls, value: object) -> object:
        """Treat a blank environment value as unset: the file sink stays disabled.

        Activation strictness (absolute path, creatable directory, writable
        file) belongs to the diagnostics boundary, which fails closed instead
        of failing configuration loading.
        """
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del settings_cls, env_settings, dotenv_settings, file_secret_settings
        return (init_settings,)


class CanonicalRecoverySettings(BaseModel):
    """Frozen, validated snapshot of the canonical recovery configuration.

    The snapshot carries the operation environment and the operator-owned
    private backup root, and nothing else. The backup root may reveal host
    layout, so it stays out of every rendered diagnostic: both ``__repr__``
    and ``__str__`` return a constant redacted token (spec 14).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    environment: RuntimeEnvironment = RuntimeEnvironment.LOCAL
    backup_root: Path

    @field_validator("backup_root")
    @classmethod
    def require_absolute_backup_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("backup root must be absolute")
        return value

    def __repr__(self) -> str:
        return "CanonicalRecoverySettings(redacted)"

    def __str__(self) -> str:
        return "CanonicalRecoverySettings(redacted)"
