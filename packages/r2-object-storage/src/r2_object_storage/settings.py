"""Frozen R2 object-storage settings and the secret-file credential boundary.

This module owns the Cloudflare R2 configuration snapshot for the concrete
adapter. It composes the shared runtime-configuration environment-name
registry, the bounded secret-file loader and the object-storage typed errors
into a frozen :class:`ObjectStorageSettings` value plus a short-lived frozen
:class:`LoadedR2Credentials` value read from two secret files.

It lives in the provider package: it imports the shared core error, settings
and secret-file contracts, but the core package never imports it. There is no
``.env``, ambient AWS credential chain, shared AWS credentials file, plaintext
secret environment variable or provider fallback; the only inputs are the
passed environment mapping and the two bounded secret files resolved beneath
``KNOWLEDGE_SECRET_ROOT``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    ValidationError,
    field_validator,
)

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.runtime_configuration.environment_names import (
    KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES,
)
from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.runtime_configuration.secret_files import read_secret_file

_ENVIRONMENT_PREFIX: Final[str] = "KNOWLEDGE_"
_MAXIMUM_CREDENTIAL_FILE_NAME_LENGTH: Final[int] = 255

# Exactly ``https://<account-id>.r2.cloudflarestorage.com`` where the account id
# is 32 lowercase hexadecimal characters, with no username, password, port,
# path, query or fragment. HTTP and custom S3 endpoints are rejected. The
# validators use ``fullmatch``, so no ``^``/``$`` anchors appear in the pattern.
_R2_ENDPOINT_PATTERN: Final = re.compile(r"https://[0-9a-f]{32}\.r2\.cloudflarestorage\.com")

# Lowercase R2-compatible bucket names from 3 through 63 characters: letters,
# digits and internal hyphens, opening and closing with a letter or digit so a
# leading or trailing hyphen, uppercase and non-bucket characters are rejected.
# The validators use ``fullmatch``, so no ``^``/``$`` anchors appear.
_R2_BUCKET_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")

#: Closed map of object-storage environment names to model field names. The key
#: set is the authoritative owned-fragment list; the repository-wide registry in
#: :mod:`personal_os.runtime_configuration.environment_names` mirrors it.
OBJECT_STORAGE_ENVIRONMENT_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "KNOWLEDGE_ENVIRONMENT": "environment",
        "KNOWLEDGE_SECRET_ROOT": "secret_root",
        "KNOWLEDGE_R2_ENDPOINT": "r2_endpoint",
        "KNOWLEDGE_R2_BUCKET_NAME": "r2_bucket_name",
        "KNOWLEDGE_R2_ACCESS_KEY_ID_FILE": "r2_access_key_id_file",
        "KNOWLEDGE_R2_SECRET_ACCESS_KEY_FILE": "r2_secret_access_key_file",
        "KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT": "object_storage_spool_root",
    }
)


def _require_single_relative_file_name(value: str) -> str:
    """Reject any credential file name that is not a single relative component.

    A separator, absolute path, ``.``, ``..``, NUL or empty value is invalid so
    the secret-file loader can never resolve a value outside ``secret_root``.
    """
    if not value:
        raise ValueError("credential file name must not be empty")
    if "\x00" in value:
        raise ValueError("credential file name must not contain NUL")
    if value in (".", ".."):
        raise ValueError("credential file name must be a regular file name")
    if "/" in value or "\\" in value:
        raise ValueError("credential file name must be a single component")
    if len(value) > _MAXIMUM_CREDENTIAL_FILE_NAME_LENGTH:
        raise ValueError("credential file name exceeds the accepted length")
    return value


class ObjectStorageSettings(BaseModel):
    """Frozen, validated snapshot of the canonical R2 object-storage connection.

    The snapshot carries only non-secret configuration: endpoint, bucket, the
    two bounded credential filenames and the spool root. Secret values never
    enter this model; they are read into a separate
    :class:`LoadedR2Credentials` only while constructing an SDK client.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    environment: RuntimeEnvironment = RuntimeEnvironment.LOCAL
    secret_root: Path = Path("/run/secrets")
    """Linux serve contract — Windows hosts always set KNOWLEDGE_SECRET_ROOT."""
    r2_endpoint: str
    r2_bucket_name: str
    r2_access_key_id_file: str
    r2_secret_access_key_file: str
    object_storage_spool_root: Path

    @field_validator("secret_root")
    @classmethod
    def _require_absolute_secret_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("secret root must be absolute")
        return value

    @field_validator("r2_endpoint")
    @classmethod
    def _require_canonical_r2_endpoint(cls, value: str) -> str:
        if _R2_ENDPOINT_PATTERN.fullmatch(value) is None:
            raise ValueError("endpoint must be the canonical R2 HTTPS URL")
        return value

    @field_validator("r2_bucket_name")
    @classmethod
    def _require_canonical_bucket_name(cls, value: str) -> str:
        if _R2_BUCKET_PATTERN.fullmatch(value) is None:
            raise ValueError("bucket name must be lowercase R2-compatible")
        return value

    @field_validator("r2_access_key_id_file")
    @classmethod
    def _require_access_key_id_file_name(cls, value: str) -> str:
        return _require_single_relative_file_name(value)

    @field_validator("r2_secret_access_key_file")
    @classmethod
    def _require_secret_access_key_file_name(cls, value: str) -> str:
        return _require_single_relative_file_name(value)

    @field_validator("object_storage_spool_root")
    @classmethod
    def _require_absolute_existing_spool_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("spool root must be absolute")
        if not value.exists():
            raise ValueError("spool root must exist")
        if not value.is_dir():
            raise ValueError("spool root must be a directory")
        return value

    def __repr__(self) -> str:
        return "ObjectStorageSettings(redacted)"

    def __str__(self) -> str:
        return "ObjectStorageSettings(redacted)"


@dataclass(frozen=True, slots=True)
class LoadedR2Credentials:
    """Short-lived frozen pair of R2 secret values read from secret files.

    The values live as :class:`SecretStr` instances so a stray ``repr`` or
    diagnostic never renders the plaintext. The pair is constructed only while
    building an SDK client and never enters a settings representation or
    diagnostic payload.
    """

    access_key_id: SecretStr
    secret_access_key: SecretStr


def load_object_storage_settings(
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[ObjectStorageSettings, LoadedR2Credentials]:
    """Load frozen R2 settings and read both credentials from secret files.

    ``environ`` defaults to ``os.environ`` read at call time (never at import
    time). A ``KNOWLEDGE_*`` key outside the repository-wide known-name registry
    raises :class:`ConfigurationError` without echoing the name or value;
    registered names owned by another fragment (runtime, database) are ignored.
    Every Pydantic validation failure maps to :class:`ObjectStorageError` with
    ``object_storage_configuration_invalid`` and no echoed input. Both
    credential files are then read through :func:`read_secret_file` beneath
    ``secret_root`` into a short-lived frozen :class:`LoadedR2Credentials`; the
    existing missing/out-of-root/insecure contract propagates unchanged.
    Ambient AWS variables have no effect because the loader reads only the
    passed mapping and the two secret files.
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
        for environment_name, field_name in OBJECT_STORAGE_ENVIRONMENT_FIELDS.items()
        if environment_name in source
    }
    try:
        # Pydantic validates the heterogeneous env values at runtime; mypy cannot
        # statically prove the dynamic dict maps onto the typed model fields.
        settings = ObjectStorageSettings(**values)  # type: ignore[arg-type]
    except ValidationError as cause:
        field_names = tuple(
            SafeToken.parse(str(error["loc"][0]))
            for error in cause.errors(include_input=False, include_url=False)
            if error["loc"]
        )
        raise ObjectStorageError(
            ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID,
            safe_details={"count": len(cause.errors()), "field_names": field_names},
        ) from cause

    access_key_id = read_secret_file(
        settings.secret_root / settings.r2_access_key_id_file,
        settings.secret_root,
    )
    secret_access_key = read_secret_file(
        settings.secret_root / settings.r2_secret_access_key_file,
        settings.secret_root,
    )
    return settings, LoadedR2Credentials(
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
    )
