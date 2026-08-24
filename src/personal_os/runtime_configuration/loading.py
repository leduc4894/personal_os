"""Exact environment snapshot loading with safe error mapping."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

from pydantic import ValidationError

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.environment_names import (
    KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES,
)
from personal_os.runtime_configuration.models import (
    CanonicalRecoverySettings,
    RuntimeSettings,
    ServiceName,
)

ENVIRONMENT_PREFIX: Final[str] = "KNOWLEDGE_"
ENVIRONMENT_FIELDS: Final[Mapping[str, str]] = {
    "KNOWLEDGE_ENVIRONMENT": "environment",
    "KNOWLEDGE_LOG_LEVEL": "log_level",
    "KNOWLEDGE_SECRET_ROOT": "secret_root",
    "KNOWLEDGE_DIAGNOSTICS_LOG_DIR": "diagnostics_log_dir",
}

#: Canonical recovery fragment: the field map owned by
#: :func:`load_canonical_recovery_settings`. The key set mirrors the
#: ``CANONICAL_RECOVERY_ENVIRONMENT_NAMES`` fragment exactly.
CANONICAL_RECOVERY_ENVIRONMENT_FIELDS: Final[Mapping[str, str]] = {
    "KNOWLEDGE_ENVIRONMENT": "environment",
    "KNOWLEDGE_CANONICAL_BACKUP_ROOT": "backup_root",
}


def load_runtime_settings(
    service_name: ServiceName,
    *,
    environ: Mapping[str, str] | None = None,
) -> RuntimeSettings:
    """Load a frozen :class:`RuntimeSettings` from an exact environment snapshot.

    ``environ`` defaults to ``os.environ`` read at call time (never at import
    time). A ``KNOWLEDGE_``-prefixed key outside the repository-wide known-name
    registry raises :class:`ConfigurationError` without echoing the offending
    name or value. Registered names owned by another fragment (database,
    object storage) are ignored; only this fragment's own field map is parsed.
    """
    source = dict(os.environ if environ is None else environ)
    unknown_count = sum(
        key.startswith(ENVIRONMENT_PREFIX) and key not in KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES
        for key in source
    )
    if unknown_count:
        raise ConfigurationError(
            ErrorCode.CONFIGURATION_UNKNOWN_KEY,
            safe_details={"count": unknown_count},
        )
    values = {
        field_name: source[environment_name]
        for environment_name, field_name in ENVIRONMENT_FIELDS.items()
        if environment_name in source
    }
    try:
        # Pydantic validates the heterogeneous env values at runtime; mypy cannot
        # statically prove the dynamic dict maps onto the typed model fields.
        return RuntimeSettings(service_name=service_name, **values)  # type: ignore[arg-type]
    except ValidationError as cause:
        field_names = tuple(
            SafeToken.parse(str(error["loc"][0]))
            for error in cause.errors(include_input=False, include_url=False)
            if error["loc"]
        )
        mapped = ConfigurationError(
            ErrorCode.CONFIGURATION_INVALID,
            safe_details={"count": len(cause.errors()), "field_names": field_names},
        )
        raise mapped from cause


def load_canonical_recovery_settings(
    *,
    environ: Mapping[str, str] | None = None,
) -> CanonicalRecoverySettings:
    """Load a frozen :class:`CanonicalRecoverySettings` snapshot.

    ``environ`` defaults to ``os.environ`` read at call time (never at import
    time). A ``KNOWLEDGE_``-prefixed key outside the repository-wide known-name
    registry raises :class:`ConfigurationError` without echoing the offending
    name or value; registered names owned by another fragment (runtime,
    database, object storage, temporal) are ignored. A missing or relative
    backup root maps to ``configuration_invalid`` with registered field names
    only; the backup-root value itself never enters an error detail.
    """
    source = dict(os.environ if environ is None else environ)
    unknown_count = sum(
        key.startswith(ENVIRONMENT_PREFIX) and key not in KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES
        for key in source
    )
    if unknown_count:
        raise ConfigurationError(
            ErrorCode.CONFIGURATION_UNKNOWN_KEY,
            safe_details={"count": unknown_count},
        )
    values = {
        field_name: source[environment_name]
        for environment_name, field_name in CANONICAL_RECOVERY_ENVIRONMENT_FIELDS.items()
        if environment_name in source
    }
    try:
        # Pydantic validates the heterogeneous env values at runtime; mypy cannot
        # statically prove the dynamic dict maps onto the typed model fields.
        return CanonicalRecoverySettings(**values)  # type: ignore[arg-type]
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
