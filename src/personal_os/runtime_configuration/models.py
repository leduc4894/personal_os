"""Frozen runtime settings model and its bounded value enums."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import field_validator
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

    @field_validator("secret_root")
    @classmethod
    def require_absolute_secret_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("secret root must be absolute")
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
