"""Frozen API server bind settings loaded from the approved environment fragment.

This module owns the API process bind configuration snapshot. It composes the
shared runtime-configuration environment-name registry with a frozen
:class:`ApiServerSettings` value. The only inputs are the passed environment
mapping and the loopback defaults pinned here: ``127.0.0.1:8000`` applies only
to the local and test environments, while staging and production refuse to
load without an explicit host and port.

It lives in the API composition-root package: it imports the shared core error
and runtime-configuration contracts, and it never imports FastAPI or Uvicorn,
so shell-only command paths stay free of server machinery.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.environment_names import (
    KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES,
)
from personal_os.runtime_configuration.models import RuntimeEnvironment

_ENVIRONMENT_PREFIX: Final[str] = "KNOWLEDGE_"

#: Loopback defaults pinned for the non-exposed environments. The local and
#: test processes bind loopback only; staging and production must not inherit
#: this default silently, so they receive no fallback values at all.
_LOOPBACK_HOST: Final[str] = "127.0.0.1"
_LOOPBACK_PORT: Final[int] = 8000
_LOOPBACK_DEFAULT_ENVIRONMENTS: Final[tuple[RuntimeEnvironment, ...]] = (
    RuntimeEnvironment.LOCAL,
    RuntimeEnvironment.TEST,
)

#: Sensible upper bound for the safe host field, tracking the FQDN boundary
#: the database settings screen uses (253 characters).
_MAXIMUM_HOST_LENGTH: Final[int] = 253

#: Closed map of API server environment names to model field names. The key
#: set mirrors the core API server fragment exactly, so the repository-wide
#: registry stays the single source of truth for the approved names.
API_SERVER_ENVIRONMENT_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "KNOWLEDGE_ENVIRONMENT": "environment",
        "KNOWLEDGE_API_HOST": "host",
        "KNOWLEDGE_API_PORT": "port",
    }
)


class ApiServerSettings(BaseModel):
    """Frozen, validated snapshot of the API process bind configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    environment: RuntimeEnvironment
    host: str
    port: int

    @field_validator("host")
    @classmethod
    def _require_safe_host(cls, value: str) -> str:
        """Trim, bound and screen the bind host.

        Leading/trailing whitespace is trimmed; the trimmed value must be
        non-empty, within ``_MAXIMUM_HOST_LENGTH`` and free of whitespace and
        control characters, mirroring the database settings host screen.
        """
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("host must not be empty")
        if len(trimmed) > _MAXIMUM_HOST_LENGTH:
            raise ValueError("host exceeds the accepted length")
        if any(character.isspace() or not character.isprintable() for character in trimmed):
            raise ValueError("host contains an unsupported character")
        return trimmed

    @field_validator("port")
    @classmethod
    def _require_port_in_bind_range(cls, value: int) -> int:
        if value < 1 or value > 65535:
            raise ValueError("port must be within the TCP bind range")
        return value


def load_api_server_settings(
    *,
    environ: Mapping[str, str] | None = None,
) -> ApiServerSettings:
    """Load a frozen :class:`ApiServerSettings` from an environment snapshot.

    ``environ`` defaults to ``os.environ`` read at call time (never at import
    time). Any ``KNOWLEDGE_*`` key outside the repository-wide known-name
    registry raises :class:`ConfigurationError` without echoing the offending
    name or value; registered names owned by another fragment (runtime,
    database, object storage, temporal) are ignored. The loopback defaults
    apply only when the environment is local or test, so staging and
    production surfaces the missing fields as ``configuration_invalid`` with
    registered field names only.
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
        for environment_name, field_name in API_SERVER_ENVIRONMENT_FIELDS.items()
        if environment_name in source
    }
    # RuntimeEnvironment is a StrEnum, so a raw local/test value matches the
    # enum members directly; any other value falls through to Pydantic, which
    # reports the environment field (plus the missing host/port) itself.
    if values.get("environment") in _LOOPBACK_DEFAULT_ENVIRONMENTS:
        values.setdefault("host", _LOOPBACK_HOST)
        values.setdefault("port", str(_LOOPBACK_PORT))
    try:
        # Pydantic validates the heterogeneous env values at runtime; mypy cannot
        # statically prove the dynamic dict maps onto the typed model fields.
        return ApiServerSettings(**values)  # type: ignore[arg-type]
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
