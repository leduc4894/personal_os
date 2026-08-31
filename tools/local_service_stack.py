"""Safe, typed local-service stack preconditions and subprocess boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import FrozenInstanceError, asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import date
from enum import IntEnum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Final, Never, Protocol, cast

import yaml  # type: ignore[import-untyped]

from personal_os.diagnostics.events import SafeToken

_MAX_CAPTURE_BYTES = 8192
_MIN_PORT = 1024
_MAX_PORT = 65535
_MIN_COMPOSE_VERSION: Final = (2, 30, 0)
_PREREQUISITE_TIMEOUT_SECONDS: Final = 10.0
_COMPOSE_CONFIG_TIMEOUT_SECONDS: Final = 30.0
_COMPOSE_PS_TEMPLATE: Final = (
    '{"Service":{{json .Service}},"State":{{json .State}},'
    '"Health":{{json .Health}},"ExitCode":{{json .ExitCode}}}'
)
_STACK_STARTUP_DEADLINE_SECONDS: Final = 180.0
_STACK_STATUS_TIMEOUT_SECONDS: Final = 10.0
_STACK_DOWN_TIMEOUT_SECONDS: Final = 45.0
_SEMANTIC_PROBE_TIMEOUT_SECONDS: Final = 10.0
_SEMANTIC_VERIFY_DEADLINE_SECONDS: Final = 30.0
_VOLUME_OPERATION_TIMEOUT_SECONDS: Final = 30.0
_SMOKE_MARKER_TIMEOUT_SECONDS: Final = 10.0
_SMOKE_TOKEN_BYTE_COUNT: Final = 12
_SMOKE_REDIS_OPERATION_TIMEOUT_SECONDS: Final = 30.0
_SMOKE_REDIS_RECOVERY_DEADLINE_SECONDS: Final = 30.0
_PROJECT_NAME_PATTERN = re.compile(r"knowledge-(?:local|ci-[a-z0-9][a-z0-9-]{0,40})")
_COMPOSE_VERSION_PATTERN = re.compile(
    r"(?:Docker Compose version )?v?(\d+)\.(\d+)\.(\d+)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?"
)
_SECRET_ALPHABET: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_SECRET_BYTE_COUNT: Final = 32
_MIN_DISTINCT_SECRET_CHARACTERS: Final = 8
_QDRANT_CONFIG_FILENAME: Final = "qdrant_config.yaml"
_APPLICATION_SECRET_FILE_NAME_PATTERN: Final = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$"
)
_MAXIMUM_APPLICATION_SECRET_FILE_NAME_LENGTH: Final = 128
_MAXIMUM_PREVIOUS_AUTHENTICATION_KEY_COUNT: Final = 4
_IMAGE_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_IMAGE_LOCK_KEYS: Final = frozenset({"version", "images"})
_IMAGE_LOCK_ENTRY_KEYS: Final = frozenset(
    {
        "component",
        "upstream_repository",
        "version",
        "tagged_reference",
        "manifest_digest",
        "supported_platforms",
        "verified_at",
    }
)
_SUPPORTED_IMAGE_PLATFORMS: Final = ("linux/amd64",)
_RUNTIME_SERVICE_NAMES: Final = frozenset(
    {
        "postgresql",
        "qdrant",
        "neo4j",
        "redis",
        "temporal",
        "temporal-ui",
        "temporal-cli",
    }
)
_INITIALIZER_SERVICE_NAMES: Final = frozenset(
    {
        "postgres-provision",
        "temporal-schema-setup",
        "temporal-namespace-bootstrap",
    }
)
_STABLE_CONTAINER_STATES: Final = frozenset(
    {"created", "running", "restarting", "exited", "paused", "dead", "removing"}
)
_STABLE_HEALTH_STATES: Final = frozenset({"healthy", "unhealthy", "starting", "none"})
_RESET_VOLUME_LABELS: Final = (
    "postgres-data",
    "qdrant-data",
    "neo4j-data",
    "redis-data",
    "temporal-health-tools",
)
_PROBE_SERVICES: Final = (
    "postgresql",
    "qdrant",
    "neo4j",
    "redis",
    "temporal",
    "temporal-ui",
)


class StackExitCode(IntEnum):
    """Stable exit codes for local-stack lifecycle operations."""

    OK = 0
    CLI = 2
    PREREQUISITE = 64
    CONTRACT = 65
    STARTUP = 69
    INTERNAL = 70
    READINESS = 75


_EXCEPTION_BOOKKEEPING_FIELDS: Final[frozenset[str]] = frozenset(
    {"__traceback__", "__cause__", "__context__", "__suppress_context__", "__notes__"}
)


class StackFailure(Exception):
    """A safe lifecycle failure that carries no raw dependency detail.

    Hand-rolled immutability instead of a frozen dataclass: Python 3.14's
    context machinery assigns exception bookkeeping fields (``__traceback__``
    and friends) while a failure propagates through ``with`` blocks, and a
    frozen dataclass turns that bookkeeping into ``FrozenInstanceError``,
    replacing the failure itself with a crash. ``__setattr__`` allows exactly
    those bookkeeping fields and rejects every field mutation.
    """

    exit_code: StackExitCode
    result_code: str
    diagnostic_payload: Mapping[str, object]

    def __init__(
        self,
        exit_code: StackExitCode,
        result_code: str,
        *,
        diagnostic_payload: Mapping[str, object] | None = None,
    ) -> None:
        object.__setattr__(self, "exit_code", exit_code)
        object.__setattr__(self, "result_code", result_code)
        object.__setattr__(
            self,
            "diagnostic_payload",
            MappingProxyType(dict(diagnostic_payload or {})),
        )

    def __setattr__(self, name: str, value: object) -> None:
        if name in _EXCEPTION_BOOKKEEPING_FIELDS:
            object.__setattr__(self, name, value)
            return
        raise FrozenInstanceError(f"cannot assign to field {name!r}")

    def __repr__(self) -> str:
        return f"StackFailure(exit_code={self.exit_code!r}, result_code={self.result_code!r})"

    def __str__(self) -> str:
        return self.result_code


@dataclass(frozen=True, slots=True)
class StackPaths:
    """Repository-relative paths used by the local stack."""

    repository_root: Path
    compose_file: Path
    image_lock: Path
    secret_directory: Path
    state_directory: Path


@dataclass(frozen=True, slots=True)
class PrerequisiteVersions:
    """Validated local lifecycle dependency versions and capabilities."""

    compose: tuple[int, int, int]
    engine_os: str
    engine_architecture: str


@dataclass(frozen=True, slots=True)
class _ApplicationSecretReferences:
    """Validated non-secret runtime references used only for local secret ownership."""

    current_authentication_key_id: str | None
    current_authentication_key_file: str | None
    previous_authentication_keys: tuple[tuple[str, str], ...]
    policy_signing_key_file: str | None

    @property
    def relative_paths(self) -> frozenset[str]:
        """Return the exact configured file names without exposing key material."""
        paths = {file_name for _key_id, file_name in self.previous_authentication_keys}
        if self.current_authentication_key_file is not None:
            paths.add(self.current_authentication_key_file)
        if self.policy_signing_key_file is not None:
            paths.add(self.policy_signing_key_file)
        return frozenset(paths)


@dataclass(frozen=True, slots=True)
class StackContext:
    """Immutable, project-scoped input to every local lifecycle operation."""

    paths: StackPaths
    project_name: str
    ports: Mapping[str, int]
    environment: Mapping[str, str]
    application_secret_references: _ApplicationSecretReferences = dataclass_field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "application_secret_references",
            _parse_application_secret_references(self.environment),
        )
        object.__setattr__(self, "ports", MappingProxyType(dict(self.ports)))
        object.__setattr__(
            self,
            "environment",
            MappingProxyType(sanitize_subprocess_environment(self.environment)),
        )


@dataclass(frozen=True, slots=True)
class ImageLockEntry:
    """One validated immutable registry-image lock entry."""

    component: str
    upstream_repository: str
    version: str
    tagged_reference: str
    manifest_digest: str
    supported_platforms: tuple[str, ...]
    verified_at: str

    @property
    def locked_reference(self) -> str:
        """Return the byte-identifying Compose registry reference."""
        return f"{self.tagged_reference}@{self.manifest_digest}"


class SecretKind(StrEnum):
    """The native format required by a local-service secret file."""

    PASSWORD = "password"
    NEO4J_AUTH = "neo4j_auth"
    REDIS_ACL = "redis_acl"


class SecretSetState(StrEnum):
    """Safe observable state of the local secret directory."""

    MISSING = "missing"
    PARTIAL = "partial"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class SecretSpec:
    """One required file in the atomic local-secret set."""

    filename: str
    kind: SecretKind


SECRET_SPECS: Final = (
    SecretSpec("postgres_admin_password", SecretKind.PASSWORD),
    SecretSpec("postgres_application_password", SecretKind.PASSWORD),
    SecretSpec("postgres_temporal_password", SecretKind.PASSWORD),
    SecretSpec("qdrant_api_key", SecretKind.PASSWORD),
    SecretSpec("neo4j_auth", SecretKind.NEO4J_AUTH),
    SecretSpec("redis_acl", SecretKind.REDIS_ACL),
    SecretSpec("redis_application_password", SecretKind.PASSWORD),
)

_SECRET_FILENAMES: Final = frozenset(spec.filename for spec in SECRET_SPECS) | {
    _QDRANT_CONFIG_FILENAME
}

_APPLICATION_SECRET_FILENAMES: Final = frozenset(
    {
        "auth-key-2026-08.key",
        "policy_signing_a.pem",
        "policy_signing_b.pem",
        "r2_access_key_id",
        "r2_secret_access_key",
        "web-credential-password.key",
    }
)


def _secret_relative_path_identity(value: str) -> str:
    """Return filesystem identity without changing identifier semantics.

    The configured grammar already admits only slash-separated ASCII paths.
    Windows path comparison is case-insensitive, while POSIX keeps exact case;
    authentication key IDs never pass through this function.
    """

    return value.casefold() if sys.platform == "win32" else value


def _validate_application_secret_relative_path(value: str) -> str:
    managed_path_identities = {
        _secret_relative_path_identity(filename) for filename in _SECRET_FILENAMES
    }
    if (
        len(value) > _MAXIMUM_APPLICATION_SECRET_FILE_NAME_LENGTH
        or _APPLICATION_SECRET_FILE_NAME_PATTERN.fullmatch(value) is None
        or _secret_relative_path_identity(value) in managed_path_identities
    ):
        raise StackFailure(StackExitCode.CONTRACT, "application_secret_configuration_invalid")
    return value


def _validate_application_secret_key_id(value: str) -> str:
    try:
        return SafeToken.parse(value).value
    except ValueError:
        raise StackFailure(
            StackExitCode.CONTRACT, "application_secret_configuration_invalid"
        ) from None


def _parse_application_secret_references(
    environment: Mapping[str, str],
) -> _ApplicationSecretReferences:
    """Parse only bounded key identifiers and file names from runtime configuration."""
    current_authentication_key_id = environment.get("KNOWLEDGE_AUTH_CURRENT_KEY_ID")
    if current_authentication_key_id is not None:
        current_authentication_key_id = _validate_application_secret_key_id(
            current_authentication_key_id
        )
    current_authentication_path = environment.get("KNOWLEDGE_AUTH_CURRENT_KEY_FILE")
    if current_authentication_path is not None:
        current_authentication_path = _validate_application_secret_relative_path(
            current_authentication_path
        )
    policy_signing_key_file = environment.get("KNOWLEDGE_POLICY_SIGNING_KEY_FILE")
    if policy_signing_key_file is not None:
        policy_signing_key_file = _validate_application_secret_relative_path(
            policy_signing_key_file
        )

    previous_keys = environment.get("KNOWLEDGE_AUTH_PREVIOUS_KEYS")
    parsed_previous_keys: list[tuple[str, str]] = []
    if previous_keys is not None and previous_keys != "":
        entries = previous_keys.split(",")
        if len(entries) > _MAXIMUM_PREVIOUS_AUTHENTICATION_KEY_COUNT:
            raise StackFailure(StackExitCode.CONTRACT, "application_secret_configuration_invalid")
        key_ids: set[str] = set()
        previous_path_identities: set[str] = set()
        for entry in entries:
            key_id, separator, file_name = entry.partition("=")
            if not separator or "=" in file_name:
                raise StackFailure(
                    StackExitCode.CONTRACT, "application_secret_configuration_invalid"
                )
            key_id = _validate_application_secret_key_id(key_id)
            relative_path = _validate_application_secret_relative_path(file_name)
            relative_path_identity = _secret_relative_path_identity(relative_path)
            if key_id in key_ids or relative_path_identity in previous_path_identities:
                raise StackFailure(
                    StackExitCode.CONTRACT, "application_secret_configuration_invalid"
                )
            key_ids.add(key_id)
            previous_path_identities.add(relative_path_identity)
            parsed_previous_keys.append((key_id, relative_path))
        if current_authentication_key_id in key_ids:
            raise StackFailure(StackExitCode.CONTRACT, "application_secret_configuration_invalid")
        if (
            current_authentication_path is not None
            and _secret_relative_path_identity(current_authentication_path)
            in previous_path_identities
        ):
            raise StackFailure(StackExitCode.CONTRACT, "application_secret_configuration_invalid")
    authentication_path_identities = {
        _secret_relative_path_identity(file_name) for _key_id, file_name in parsed_previous_keys
    }
    if current_authentication_path is not None:
        authentication_path_identities.add(
            _secret_relative_path_identity(current_authentication_path)
        )
    if (
        policy_signing_key_file is not None
        and _secret_relative_path_identity(policy_signing_key_file)
        in authentication_path_identities
    ):
        raise StackFailure(StackExitCode.CONTRACT, "application_secret_configuration_invalid")
    return _ApplicationSecretReferences(
        current_authentication_key_id=current_authentication_key_id,
        current_authentication_key_file=current_authentication_path,
        previous_authentication_keys=tuple(parsed_previous_keys),
        policy_signing_key_file=policy_signing_key_file,
    )


def _resolve_application_secret_references(
    *,
    environment: Mapping[str, str] | None,
    application_secret_references: _ApplicationSecretReferences | None,
) -> _ApplicationSecretReferences:
    if environment is not None and application_secret_references is not None:
        raise StackFailure(StackExitCode.CONTRACT, "application_secret_configuration_invalid")
    if application_secret_references is not None:
        return application_secret_references
    return _parse_application_secret_references(os.environ if environment is None else environment)


@dataclass(frozen=True, slots=True)
class PortBinding:
    """One allowed host-to-container local-stack port mapping."""

    variable: str
    default: int
    container_port: int


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded, non-throwing subprocess result."""

    return_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One redacted authenticated semantic-readiness result."""

    service: str
    is_ready: bool
    result_code: str
    latency_ms: int


@dataclass(frozen=True, slots=True)
class SmokeMarkerSet:
    """Safe test-only names and identifiers for one disposable smoke run."""

    marker_key: str
    qdrant_collection: str
    qdrant_point_id: int
    redis_key: str


class CommandRunner(Protocol):
    """Callable contract for bounded lifecycle command execution."""

    def __call__(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class _SmokeOperations(Protocol):
    def reset_before(self, context: StackContext) -> None: ...

    def bootstrap(self, context: StackContext) -> None: ...

    def config(self, context: StackContext) -> None: ...

    def up(self, context: StackContext) -> None: ...

    def verify(self, context: StackContext) -> None: ...

    def new_markers(self, context: StackContext) -> SmokeMarkerSet: ...

    def create_markers(self, context: StackContext, markers: SmokeMarkerSet) -> None: ...

    def down_preserve(self, context: StackContext) -> None: ...

    def verify_markers(self, context: StackContext, markers: SmokeMarkerSet) -> None: ...

    def stop_redis(self, context: StackContext) -> None: ...

    def verify_outage(self, context: StackContext) -> None: ...

    def start_redis(self, context: StackContext) -> None: ...

    def verify_recovery(self, context: StackContext) -> None: ...

    def ensure_redis_recovered(self, context: StackContext) -> None: ...

    def remove_markers(self, context: StackContext, markers: SmokeMarkerSet) -> None: ...

    def reset_after(self, context: StackContext) -> None: ...


class _SocketHandle(Protocol):
    def setsockopt(self, level: int, option: int, value: int) -> None: ...

    def bind(self, address: tuple[str, int]) -> None: ...

    def close(self) -> None: ...


_SocketFactory = Callable[[int, int], _SocketHandle]

PORT_BINDINGS: tuple[PortBinding, ...] = (
    PortBinding(variable="POSTGRES_PORT", default=5432, container_port=5432),
    PortBinding(variable="QDRANT_HTTP_PORT", default=6333, container_port=6333),
    PortBinding(variable="QDRANT_GRPC_PORT", default=6334, container_port=6334),
    PortBinding(variable="NEO4J_HTTP_PORT", default=7474, container_port=7474),
    PortBinding(variable="NEO4J_BOLT_PORT", default=7687, container_port=7687),
    PortBinding(variable="REDIS_PORT", default=6379, container_port=6379),
    PortBinding(variable="TEMPORAL_GRPC_PORT", default=7233, container_port=7233),
    PortBinding(variable="TEMPORAL_UI_PORT", default=8080, container_port=8080),
)

_PORT_VARIABLES = frozenset(binding.variable for binding in PORT_BINDINGS)
_ALLOWED_SUBPROCESS_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PROGRAMFILES",
        "ProgramFiles",
        "TMP",
        "TEMP",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "CI",
        *_PORT_VARIABLES,
    }
)


def load_image_lock(image_lock_path: Path) -> tuple[ImageLockEntry, ...]:
    """Parse and strictly validate the local-stack immutable image lock."""
    try:
        loaded: object = yaml.safe_load(image_lock_path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, yaml.YAMLError:
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid") from None
    if not isinstance(loaded, dict):
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid")
    document = cast(dict[object, object], loaded)
    if set(document) != _IMAGE_LOCK_KEYS or type(document["version"]) is not int:
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid")
    if document["version"] != 1:
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid")
    raw_images = document["images"]
    if not isinstance(raw_images, list) or not raw_images:
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid")

    entries: list[ImageLockEntry] = []
    components: set[str] = set()
    tagged_references: set[str] = set()
    for raw_entry in raw_images:
        entry = _parse_image_lock_entry(raw_entry)
        if entry.component in components or entry.tagged_reference in tagged_references:
            raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid")
        components.add(entry.component)
        tagged_references.add(entry.tagged_reference)
        entries.append(entry)
    return tuple(entries)


def validate_image_lock(paths: StackPaths) -> tuple[ImageLockEntry, ...]:
    """Require every Compose image to agree exactly with the immutable lock."""
    entries = load_image_lock(paths.image_lock)
    try:
        loaded: object = yaml.safe_load(paths.compose_file.read_text(encoding="utf-8"))
    except OSError, UnicodeError, yaml.YAMLError:
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_mismatch") from None
    if not isinstance(loaded, dict):
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_mismatch")
    document = cast(dict[object, object], loaded)
    services = document.get("services")
    if not isinstance(services, dict) or not services:
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_mismatch")

    compose_references: set[str] = set()
    for raw_service in services.values():
        if not isinstance(raw_service, dict):
            raise StackFailure(StackExitCode.CONTRACT, "image_lock_mismatch")
        service = cast(dict[object, object], raw_service)
        image = service.get("image")
        if not isinstance(image, str) or service.get("platform") != "linux/amd64":
            raise StackFailure(StackExitCode.CONTRACT, "image_lock_mismatch")
        compose_references.add(image)

    locked_references = {entry.locked_reference for entry in entries}
    if compose_references != locked_references:
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_mismatch")
    return entries


def _parse_image_lock_entry(raw_entry: object) -> ImageLockEntry:
    if not isinstance(raw_entry, dict):
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid")
    entry = cast(dict[object, object], raw_entry)
    if set(entry) != _IMAGE_LOCK_ENTRY_KEYS:
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid")

    string_fields = (
        "component",
        "upstream_repository",
        "version",
        "tagged_reference",
        "manifest_digest",
        "verified_at",
    )
    values: dict[str, str] = {}
    for field in string_fields:
        value = entry[field]
        if not isinstance(value, str) or not value or value.strip() != value:
            raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid")
        values[field] = value

    repository = values["upstream_repository"]
    tagged_reference = values["tagged_reference"]
    repository_prefix = f"{repository}:"
    if (
        any(character.isspace() for character in repository)
        or "@" in repository
        or not tagged_reference.startswith(repository_prefix)
        or "@" in tagged_reference
    ):
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid")
    tag = tagged_reference[len(repository_prefix) :]
    version = values["version"]
    if tag not in {version, f"v{version}"}:
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid")
    if _IMAGE_DIGEST_PATTERN.fullmatch(values["manifest_digest"]) is None:
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid")

    raw_platforms = entry["supported_platforms"]
    if raw_platforms != list(_SUPPORTED_IMAGE_PLATFORMS):
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid")
    try:
        if date.fromisoformat(values["verified_at"]).isoformat() != values["verified_at"]:
            raise ValueError
    except ValueError:
        raise StackFailure(StackExitCode.CONTRACT, "image_lock_invalid") from None

    return ImageLockEntry(
        component=values["component"],
        upstream_repository=repository,
        version=version,
        tagged_reference=tagged_reference,
        manifest_digest=values["manifest_digest"],
        supported_platforms=_SUPPORTED_IMAGE_PLATFORMS,
        verified_at=values["verified_at"],
    )


def inspect_secret_set(
    paths: StackPaths,
    *,
    environment: Mapping[str, str] | None = None,
    application_secret_references: _ApplicationSecretReferences | None = None,
) -> SecretSetState:
    """Return the safe state of the exact local secret set without reading values."""
    references = _resolve_application_secret_references(
        environment=environment,
        application_secret_references=application_secret_references,
    )
    allowed_application_paths = _APPLICATION_SECRET_FILENAMES | references.relative_paths
    allowed_directory_path_identities = {
        _secret_relative_path_identity("/".join(relative_path.split("/")[:segment_count]))
        for relative_path in allowed_application_paths
        for segment_count in range(1, len(relative_path.split("/")))
    }
    secret_directory = _validate_secret_directory_location(paths)
    local_directory = secret_directory.parent
    local_stat = _lstat_or_failure(local_directory, "secret_set_inspection_failed")
    if local_stat is None:
        return SecretSetState.MISSING
    _validate_private_directory(local_directory, local_stat)

    secret_stat = _lstat_or_failure(secret_directory, "secret_set_inspection_failed")
    if secret_stat is None:
        return SecretSetState.MISSING
    _validate_private_directory(secret_directory, secret_stat)

    found_relative_path_identities: set[str] = set()
    child_stats: list[tuple[Path, os.stat_result]] = []

    def inspect_directory(directory: Path, relative_parts: tuple[str, ...]) -> None:
        try:
            children = tuple(directory.iterdir())
        except OSError:
            raise StackFailure(StackExitCode.CONTRACT, "secret_set_inspection_failed") from None
        for child in children:
            child_stat = _lstat_or_failure(child, "secret_set_inspection_failed")
            if child_stat is None:
                raise StackFailure(StackExitCode.CONTRACT, "secret_set_inspection_failed")
            relative_path = "/".join((*relative_parts, child.name))
            if stat.S_ISDIR(child_stat.st_mode) and not stat.S_ISLNK(child_stat.st_mode):
                _validate_private_directory(child, child_stat)
                child_stats.append((child, child_stat))
                relative_path_identity = _secret_relative_path_identity(relative_path)
                if relative_path_identity not in allowed_directory_path_identities:
                    found_relative_path_identities.add(relative_path_identity)
                    continue
                inspect_directory(child, (*relative_parts, child.name))
                continue
            _validate_private_file(child, child_stat)
            child_stats.append((child, child_stat))
            found_relative_path_identities.add(_secret_relative_path_identity(relative_path))

    inspect_directory(secret_directory, ())

    allowed_relative_path_identities = {
        _secret_relative_path_identity(relative_path)
        for relative_path in _SECRET_FILENAMES | allowed_application_paths
    }
    if not found_relative_path_identities <= allowed_relative_path_identities:
        return SecretSetState.PARTIAL
    _validate_private_directory(local_directory, local_stat, require_mode=True)
    _validate_private_directory(secret_directory, secret_stat, require_mode=True)
    for child, child_stat in child_stats:
        if stat.S_ISDIR(child_stat.st_mode):
            _validate_private_directory(child, child_stat, require_mode=True)
        else:
            _validate_private_file(child, child_stat, require_mode=True)
    managed_path_identities = {
        _secret_relative_path_identity(filename) for filename in _SECRET_FILENAMES
    }
    found_managed_path_identities = found_relative_path_identities & managed_path_identities
    if not found_managed_path_identities:
        return SecretSetState.MISSING
    if found_managed_path_identities != managed_path_identities:
        return SecretSetState.PARTIAL
    _validate_complete_secret_contents(secret_directory)
    return SecretSetState.COMPLETE


def bootstrap_secret_set(
    paths: StackPaths,
    *,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    environment: Mapping[str, str] | None = None,
    application_secret_references: _ApplicationSecretReferences | None = None,
) -> SecretSetState:
    """Atomically create or safely reuse the complete local secret set."""
    references = _resolve_application_secret_references(
        environment=environment,
        application_secret_references=application_secret_references,
    )
    state = inspect_secret_set(paths, application_secret_references=references)
    if state is SecretSetState.COMPLETE:
        return state
    if state is SecretSetState.PARTIAL:
        raise StackFailure(StackExitCode.CONTRACT, "partial_secret_set")

    secret_directory = _validate_secret_directory_location(paths)
    local_directory = secret_directory.parent
    _create_or_validate_local_directory(local_directory)
    staging_directory: Path | None = None
    created_files: list[Path] = []
    installed_files: list[Path] = []
    has_renamed_secret_set = False
    try:
        staging_directory = Path(
            tempfile.mkdtemp(prefix=".stack-secrets-staging-", dir=local_directory)
        )
        _set_private_mode(staging_directory, 0o700)
        staging_stat = _lstat_or_failure(staging_directory, "secret_set_creation_failed")
        if staging_stat is None:
            raise StackFailure(StackExitCode.CONTRACT, "secret_set_creation_failed")
        _validate_private_directory(staging_directory, staging_stat, require_mode=True)

        secret_contents = _build_secret_contents(random_bytes)
        for filename, content in secret_contents.items():
            secret_path = staging_directory / filename
            created_files.append(secret_path)
            _write_private_secret_file(secret_path, content)
        _flush_directory(staging_directory)
        secret_stat = _lstat_or_failure(secret_directory, "secret_set_creation_failed")
        if secret_stat is None:
            os.rename(staging_directory, secret_directory)
            staging_directory = None
            has_renamed_secret_set = True
        else:
            _validate_private_directory(secret_directory, secret_stat, require_mode=True)
            for staged_path in created_files:
                installed_path = secret_directory / staged_path.name
                os.rename(staged_path, installed_path)
                installed_files.append(installed_path)
            staging_directory.rmdir()
            staging_directory = None
            _flush_directory(secret_directory)
        _flush_directory(local_directory)
    except StackFailure:
        if has_renamed_secret_set or installed_files:
            try:
                if (
                    inspect_secret_set(paths, application_secret_references=references)
                    is SecretSetState.COMPLETE
                ):
                    return SecretSetState.COMPLETE
            except StackFailure:
                pass
        for installed_file in reversed(installed_files):
            with suppress(OSError):
                installed_file.unlink()
        raise
    except OSError, ValueError:
        for installed_file in reversed(installed_files):
            with suppress(OSError):
                installed_file.unlink()
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_creation_failed") from None
    finally:
        if staging_directory is not None:
            _remove_staging_files(staging_directory, created_files)

    state = inspect_secret_set(paths, application_secret_references=references)
    if state is not SecretSetState.COMPLETE:
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_creation_failed")
    return state


def validate_secret_set(
    paths: StackPaths,
    *,
    list_project_volumes: Callable[[], Sequence[str]],
    environment: Mapping[str, str] | None = None,
    application_secret_references: _ApplicationSecretReferences | None = None,
) -> SecretSetState:
    """Refuse missing credentials when existing volumes could depend on them."""
    state = inspect_secret_set(
        paths,
        environment=environment,
        application_secret_references=application_secret_references,
    )
    if state is SecretSetState.PARTIAL:
        raise StackFailure(StackExitCode.CONTRACT, "partial_secret_set")
    if state is SecretSetState.MISSING:
        try:
            has_project_volumes = bool(tuple(list_project_volumes()))
        except Exception:
            raise StackFailure(StackExitCode.PREREQUISITE, "volume_inspection_failed") from None
        if has_project_volumes:
            raise StackFailure(StackExitCode.CONTRACT, "secret_set_missing_with_volumes")
    return state


def remove_secret_set_after_reset(
    paths: StackPaths,
    *,
    environment: Mapping[str, str] | None = None,
    application_secret_references: _ApplicationSecretReferences | None = None,
) -> SecretSetState:
    """Remove only managed stack secrets after the project reset succeeds."""
    references = _resolve_application_secret_references(
        environment=environment,
        application_secret_references=application_secret_references,
    )
    state = inspect_secret_set(paths, application_secret_references=references)
    if state is SecretSetState.MISSING:
        return state
    if state is SecretSetState.PARTIAL:
        raise StackFailure(StackExitCode.CONTRACT, "partial_secret_set")

    secret_directory = _validate_secret_directory_location(paths)
    try:
        for filename in _SECRET_FILENAMES:
            (secret_directory / filename).unlink()
        if not any(secret_directory.iterdir()):
            secret_directory.rmdir()
        else:
            _flush_directory(secret_directory)
        _flush_directory(secret_directory.parent)
    except OSError:
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_removal_failed") from None
    if (
        inspect_secret_set(paths, application_secret_references=references)
        is not SecretSetState.MISSING
    ):
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_removal_failed")
    return SecretSetState.MISSING


def resolve_stack_paths(repository_root: Path) -> StackPaths:
    """Return canonical local-stack paths confined to ``repository_root``."""
    resolved_root = repository_root.resolve(strict=False)
    compose_file = _resolve_beneath(resolved_root, "infra", "compose", "compose.yaml")
    image_lock = _resolve_beneath(resolved_root, "infra", "compose", "images.lock.yaml")
    secret_directory = _resolve_beneath(resolved_root, ".local", "stack-secrets")
    state_directory = _resolve_beneath(resolved_root, ".local", "stack-state")
    return StackPaths(
        repository_root=resolved_root,
        compose_file=compose_file,
        image_lock=image_lock,
        secret_directory=secret_directory,
        state_directory=state_directory,
    )


def validate_project_name(name: str) -> str:
    """Accept only the bounded local-stack Compose project-name contract."""
    if _PROJECT_NAME_PATTERN.fullmatch(name) is None:
        raise StackFailure(StackExitCode.CLI, "invalid_project_name")
    return name


def resolve_ports(environment: Mapping[str, str]) -> dict[str, int]:
    """Resolve and validate the eight approved host port overrides."""
    ports: dict[str, int] = {}
    for binding in PORT_BINDINGS:
        raw_port = environment.get(binding.variable)
        port = binding.default if raw_port is None else _parse_port(raw_port)
        if port in ports.values():
            raise StackFailure(StackExitCode.CLI, "duplicate_port")
        ports[binding.variable] = port
    return ports


def validate_port_availability(
    ports: Mapping[str, int], *, socket_factory: _SocketFactory | None = None
) -> None:
    """Ensure all effective host ports can bind on loopback without leaking errors."""
    create_socket = socket.socket if socket_factory is None else socket_factory
    for port in ports.values():
        candidate_socket: _SocketHandle | None = None
        is_unavailable = False
        try:
            candidate_socket = create_socket(socket.AF_INET, socket.SOCK_STREAM)
            if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                candidate_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                # Docker publishes the stack ports with SO_REUSEADDR; probe the
                # same way so TIME_WAIT sockets left by a torn-down stack cycle
                # do not read as occupied while an active listener still does.
                candidate_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate_socket.bind(("127.0.0.1", port))
        except OSError:
            is_unavailable = True
        finally:
            if candidate_socket is not None:
                try:
                    candidate_socket.close()
                except OSError:
                    is_unavailable = True
        if is_unavailable:
            raise StackFailure(StackExitCode.PREREQUISITE, "port_unavailable")


def sanitize_subprocess_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Return the explicit subprocess environment allowlist without credentials."""
    return {
        key: value
        for key, value in environment.items()
        if key in _ALLOWED_SUBPROCESS_ENVIRONMENT_KEYS
    }


def run_command(
    arguments: Sequence[str],
    *,
    timeout_seconds: float,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run an allowlisted-environment command with bounded, safe diagnostics."""
    if not arguments or any(not argument for argument in arguments):
        raise StackFailure(StackExitCode.CLI, "invalid_command")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise StackFailure(StackExitCode.CLI, "invalid_timeout")

    deadline_monotonic = time.monotonic() + timeout_seconds
    source_environment: Mapping[str, str] = os.environ if environment is None else environment
    effective_ports = resolve_ports(source_environment)
    clean_environment = sanitize_subprocess_environment(source_environment)
    clean_environment.update({variable: str(port) for variable, port in effective_ports.items()})

    process: subprocess.Popen[bytes] | None = None
    for _spawn_attempt in range(2):
        with suppress(OSError, ValueError):
            process = subprocess.Popen(
                list(arguments),
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=clean_environment,
            )
        if process is not None:
            break
        # One bounded retry: Windows can transiently refuse a spawn under
        # heavy parallel load (pipe-buffer pressure); the second failure
        # still surfaces as subprocess_unavailable, so a real outage is
        # never masked.
        if _spawn_attempt == 0:
            time.sleep(0.25)
    if process is None:
        raise StackFailure(StackExitCode.PREREQUISITE, "subprocess_unavailable")

    if process.stdout is None or process.stderr is None:
        with suppress(OSError):
            process.kill()
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=0)
        raise StackFailure(StackExitCode.INTERNAL, "subprocess_capture_unavailable")

    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    stdout_thread = threading.Thread(
        target=_read_bounded_output,
        args=(process.stdout, stdout_buffer),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_bounded_output,
        args=(process.stderr, stderr_buffer),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    has_timed_out = False
    has_wait_failure = False
    return_code: int | None = None
    try:
        remaining_seconds = max(0.0, deadline_monotonic - time.monotonic())
        return_code = process.wait(timeout=remaining_seconds)
    except subprocess.TimeoutExpired:
        has_timed_out = True
        with suppress(OSError):
            process.kill()
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=0)
    except OSError:
        has_wait_failure = True
    finally:
        _drain_readers_until_deadline((stdout_thread, stderr_thread), deadline_monotonic)

    if has_timed_out:
        raise StackFailure(StackExitCode.READINESS, "subprocess_timeout")
    if has_wait_failure or return_code is None:
        raise StackFailure(StackExitCode.PREREQUISITE, "subprocess_wait_failed")

    return CommandResult(
        return_code=return_code,
        stdout=stdout_buffer.decode("utf-8", errors="replace"),
        stderr=stderr_buffer.decode("utf-8", errors="replace"),
    )


def compose_arguments(context: StackContext) -> list[str]:
    """Build the explicit project- and file-scoped Compose command prefix."""
    return [
        "docker",
        "compose",
        "--file",
        str(context.paths.compose_file),
        "--project-name",
        context.project_name,
    ]


def check_prerequisites(
    context: StackContext,
    *,
    runner: CommandRunner = run_command,
    require_engine: bool = True,
) -> PrerequisiteVersions:
    """Require a supported Compose CLI and, when needed, Linux amd64 Docker."""
    compose_result = _run_prerequisite_command(
        runner,
        [*compose_arguments(context), "version", "--short"],
        context,
        result_code="compose_unavailable",
    )
    compose_version, compose_prerelease = _parse_compose_version(compose_result.stdout)
    if compose_version < _MIN_COMPOSE_VERSION or (
        compose_version == _MIN_COMPOSE_VERSION and compose_prerelease is not None
    ):
        raise StackFailure(StackExitCode.PREREQUISITE, "compose_version_unsupported")

    if not require_engine:
        return PrerequisiteVersions(compose_version, "", "")

    engine_result = _run_prerequisite_command(
        runner,
        ("docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"),
        context,
        result_code="engine_unavailable",
    )
    engine_capability = engine_result.stdout.strip().lower().split("/", maxsplit=1)
    if len(engine_capability) != 2 or not all(engine_capability):
        raise StackFailure(StackExitCode.PREREQUISITE, "engine_capability_invalid")
    engine_os, engine_architecture = engine_capability
    if engine_os != "linux":
        raise StackFailure(StackExitCode.PREREQUISITE, "engine_os_unsupported")
    if engine_architecture != "amd64":
        raise StackFailure(StackExitCode.PREREQUISITE, "engine_architecture_unsupported")
    return PrerequisiteVersions(compose_version, engine_os, engine_architecture)


def validate_compose_config(context: StackContext, *, runner: CommandRunner = run_command) -> None:
    """Validate the complete static stack contract without contacting the engine."""
    _validate_lifecycle_project(context)
    check_prerequisites(context, runner=runner, require_engine=False)
    validate_port_availability(context.ports)
    _require_complete_secret_set(context, runner=runner, inspect_project_volumes=False)
    validate_image_lock(context.paths)
    _run_compose_config(context, runner)


def stack_up(
    context: StackContext,
    *,
    runner: CommandRunner = run_command,
    deadline_seconds: float = _STACK_STARTUP_DEADLINE_SECONDS,
) -> dict[str, object]:
    """Run fail-fast preflight, bounded Compose startup and init inspection."""
    _validate_lifecycle_project(context)
    _validate_deadline(deadline_seconds)
    check_prerequisites(context, runner=runner)
    validate_port_availability(context.ports)
    _require_complete_secret_set(context, runner=runner, inspect_project_volumes=True)
    validate_image_lock(context.paths)
    _run_compose_config(context, runner)

    deadline_monotonic = time.monotonic() + deadline_seconds
    startup_arguments = [
        *compose_arguments(context),
        "up",
        "--detach",
        "--remove-orphans",
        "--wait",
        "--wait-timeout",
        str(int(deadline_seconds)),
    ]
    startup_result = _run_lifecycle_command(
        runner,
        startup_arguments,
        timeout_seconds=_remaining_seconds(deadline_monotonic, time.monotonic),
        context=context,
    )
    if startup_result.return_code != 0:
        raise StackFailure(
            StackExitCode.STARTUP,
            "stack_startup_failed",
            diagnostic_payload={
                "stack_status": _read_startup_failure_status(
                    context,
                    runner=runner,
                    deadline_monotonic=deadline_monotonic,
                )
            },
        )
    status = _wait_for_stack_until(
        context,
        runner=runner,
        deadline_monotonic=deadline_monotonic,
        poll_interval_seconds=1.0,
        clock=time.monotonic,
        sleep=time.sleep,
    )
    _run_semantic_probes(
        context,
        runner=runner,
        deadline_monotonic=min(
            deadline_monotonic,
            time.monotonic() + _SEMANTIC_VERIFY_DEADLINE_SECONDS,
        ),
        clock=time.monotonic,
    )
    return status


def _repeat_smoke_stack_up(
    context: StackContext,
    *,
    runner: CommandRunner,
    deadline_seconds: float = _STACK_STARTUP_DEADLINE_SECONDS,
) -> dict[str, object]:
    """Repeat Compose up only after this exact running project verifies successfully."""
    _validate_lifecycle_project(context)
    _validate_deadline(deadline_seconds)
    verify_stack(context, runner=runner)
    validate_image_lock(context.paths)
    _run_compose_config(context, runner)

    deadline_monotonic = time.monotonic() + deadline_seconds
    result = _run_lifecycle_command(
        runner,
        [
            *compose_arguments(context),
            "up",
            "--detach",
            "--remove-orphans",
            "--wait",
            "--wait-timeout",
            str(int(deadline_seconds)),
        ],
        timeout_seconds=_remaining_seconds(deadline_monotonic, time.monotonic),
        context=context,
    )
    if result.return_code != 0:
        raise StackFailure(StackExitCode.STARTUP, "stack_startup_failed")
    status = _wait_for_stack_until(
        context,
        runner=runner,
        deadline_monotonic=deadline_monotonic,
        poll_interval_seconds=1.0,
        clock=time.monotonic,
        sleep=time.sleep,
    )
    _run_semantic_probes(
        context,
        runner=runner,
        deadline_monotonic=min(
            deadline_monotonic,
            time.monotonic() + _SEMANTIC_VERIFY_DEADLINE_SECONDS,
        ),
        clock=time.monotonic,
    )
    return status


def wait_for_stack(
    context: StackContext,
    *,
    runner: CommandRunner = run_command,
    deadline_seconds: float = _STACK_STARTUP_DEADLINE_SECONDS,
    poll_interval_seconds: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Wait within one finite deadline for health and all initializers to settle."""
    _validate_lifecycle_project(context)
    _validate_deadline(deadline_seconds)
    if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
        raise StackFailure(StackExitCode.CLI, "invalid_poll_interval")
    return _wait_for_stack_until(
        context,
        runner=runner,
        deadline_monotonic=clock() + deadline_seconds,
        poll_interval_seconds=poll_interval_seconds,
        clock=clock,
        sleep=sleep,
    )


def stack_status(
    context: StackContext, *, runner: CommandRunner = run_command
) -> dict[str, object]:
    """Return only stable, non-secret lifecycle state for the exact project."""
    _validate_lifecycle_project(context)
    check_prerequisites(context, runner=runner)
    return _read_stack_status(
        context,
        runner=runner,
        timeout_seconds=_STACK_STATUS_TIMEOUT_SECONDS,
    )


def verify_stack(
    context: StackContext,
    *,
    runner: CommandRunner = run_command,
    deadline_seconds: float = _SEMANTIC_VERIFY_DEADLINE_SECONDS,
) -> tuple[ProbeResult, ...]:
    """Run only authenticated, redacted semantic probes against a ready stack."""
    _validate_lifecycle_project(context)
    _validate_deadline(deadline_seconds)
    check_prerequisites(context, runner=runner)
    deadline_monotonic = time.monotonic() + deadline_seconds
    status = _read_stack_status(
        context,
        runner=runner,
        timeout_seconds=min(
            _STACK_STATUS_TIMEOUT_SECONDS,
            _remaining_seconds(deadline_monotonic, time.monotonic),
        ),
    )
    if status["state"] == "absent":
        raise StackFailure(StackExitCode.CLI, "stack_absent")
    probes = _run_semantic_probes(
        context,
        runner=runner,
        deadline_monotonic=deadline_monotonic,
        clock=time.monotonic,
    )
    if status["state"] != "ready":
        raise StackFailure(StackExitCode.READINESS, "stack_not_ready")
    return probes


def reset_stack(
    context: StackContext,
    *,
    confirm_project: str,
    rotate_secrets: bool = False,
    runner: CommandRunner = run_command,
) -> dict[str, object]:
    """Delete only the exact five doubly-labeled project volumes after confirmation."""
    _validate_lifecycle_project(context)
    if confirm_project != context.project_name:
        raise StackFailure(StackExitCode.CLI, "reset_confirmation_mismatch")
    check_prerequisites(context, runner=runner)
    resolved_volumes = _resolve_reset_volumes(context, runner)
    if rotate_secrets and not resolved_volumes:
        raise StackFailure(StackExitCode.CONTRACT, "secret_rotation_requires_volume_deletion")

    down_result = _run_lifecycle_command(
        runner,
        [*compose_arguments(context), "down", "--remove-orphans", "--timeout", "30"],
        timeout_seconds=_STACK_DOWN_TIMEOUT_SECONDS,
        context=context,
    )
    if down_result.return_code != 0:
        raise StackFailure(StackExitCode.STARTUP, "stack_down_failed")

    resolved_after_down = _resolve_reset_volumes(context, runner)
    if resolved_after_down != resolved_volumes:
        raise StackFailure(StackExitCode.CONTRACT, "project_volume_set_changed")

    for volume_name in resolved_volumes:
        removal_result = _run_lifecycle_command(
            runner,
            ("docker", "volume", "rm", volume_name),
            timeout_seconds=_VOLUME_OPERATION_TIMEOUT_SECONDS,
            context=context,
        )
        if removal_result.return_code != 0:
            raise StackFailure(StackExitCode.STARTUP, "volume_removal_failed")

    if _list_project_volumes(context, runner):
        raise StackFailure(StackExitCode.STARTUP, "volume_removal_incomplete")

    secret_state = "preserved"
    if rotate_secrets:
        remove_secret_set_after_reset(
            context.paths,
            application_secret_references=context.application_secret_references,
        )
        secret_state = "removed"
    return {
        "project": context.project_name,
        "state": "absent",
        "removed_volumes": len(resolved_volumes),
        "secrets": secret_state,
        "result_code": "stack_reset_complete",
    }


def stack_down(context: StackContext, *, runner: CommandRunner = run_command) -> None:
    """Remove only project containers and network while preserving all volumes/secrets."""
    _validate_lifecycle_project(context)
    check_prerequisites(context, runner=runner)
    result = _run_lifecycle_command(
        runner,
        [*compose_arguments(context), "down", "--remove-orphans", "--timeout", "30"],
        timeout_seconds=_STACK_DOWN_TIMEOUT_SECONDS,
        context=context,
    )
    if result.return_code != 0:
        raise StackFailure(StackExitCode.STARTUP, "stack_down_failed")


def run_smoke_contract(
    context: StackContext,
    *,
    operations: _SmokeOperations | None = None,
    runner: CommandRunner = run_command,
) -> None:
    """Run the CI-only disposable smoke sequence with guaranteed final reset."""
    if context.environment.get("CI") != "true" or not context.project_name.startswith(
        "knowledge-ci-"
    ):
        raise StackFailure(StackExitCode.CLI, "smoke_requires_ci_project")
    if operations is None:
        operations = _DefaultSmokeOperations(runner)

    markers: SmokeMarkerSet | None = None
    primary_failure: BaseException | None = None
    is_redis_recovery_required = False
    try:
        operations.reset_before(context)
        operations.bootstrap(context)
        operations.config(context)
        operations.up(context)
        operations.verify(context)
        markers = operations.new_markers(context)
        operations.create_markers(context, markers)
        operations.down_preserve(context)
        operations.up(context)
        operations.verify(context)
        operations.verify_markers(context, markers)
        operations.up(context)
        operations.verify(context)
        is_redis_recovery_required = True
        operations.stop_redis(context)
        operations.verify_outage(context)
        operations.start_redis(context)
        operations.verify_recovery(context)
        is_redis_recovery_required = False
    except BaseException as failure:
        primary_failure = failure

    if is_redis_recovery_required:
        try:
            operations.ensure_redis_recovered(context)
        except BaseException as recovery_failure:
            if primary_failure is None:
                raise recovery_failure
            raise primary_failure from recovery_failure

    marker_cleanup_failure: BaseException | None = None
    try:
        if markers is not None:
            operations.remove_markers(context, markers)
    except BaseException as failure:
        marker_cleanup_failure = failure

    reset_failure: BaseException | None = None
    try:
        operations.reset_after(context)
    except BaseException as failure:
        reset_failure = failure

    if primary_failure is not None:
        raise primary_failure
    if reset_failure is not None:
        raise reset_failure
    if marker_cleanup_failure is not None:
        raise marker_cleanup_failure


def _create_smoke_markers(
    context: StackContext,
    *,
    runner: CommandRunner,
    markers: SmokeMarkerSet | None = None,
    token_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> SmokeMarkerSet:
    markers = _new_smoke_markers(token_bytes=token_bytes) if markers is None else markers
    _validate_smoke_markers(markers)
    try:
        _run_smoke_marker_commands(
            context,
            runner=runner,
            commands=_smoke_marker_commands(markers, stage="create"),
            success_code="smoke_markers_created",
            failure_code="smoke_marker_create_failed",
        )
    except StackFailure:
        with suppress(StackFailure):
            _remove_smoke_markers(context, markers, runner=runner)
        raise
    return markers


def _new_smoke_markers(
    *,
    token_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> SmokeMarkerSet:
    token = token_bytes(_SMOKE_TOKEN_BYTE_COUNT).hex()
    markers = SmokeMarkerSet(
        marker_key=token,
        qdrant_collection=f"stack_smoke_marker_{token}",
        qdrant_point_id=1,
        redis_key=f"stack:smoke:{token}",
    )
    _validate_smoke_markers(markers)
    return markers


def _verify_smoke_markers(
    context: StackContext,
    markers: SmokeMarkerSet,
    *,
    runner: CommandRunner,
) -> None:
    _validate_smoke_markers(markers)
    _run_smoke_marker_commands(
        context,
        runner=runner,
        commands=_smoke_marker_commands(markers, stage="verify"),
        success_code="smoke_markers_verified",
        failure_code="smoke_marker_verify_failed",
    )


def _remove_smoke_markers(
    context: StackContext,
    markers: SmokeMarkerSet,
    *,
    runner: CommandRunner,
) -> None:
    _validate_smoke_markers(markers)
    _run_smoke_marker_commands(
        context,
        runner=runner,
        commands=_smoke_marker_commands(markers, stage="remove"),
        success_code="smoke_markers_removed",
        failure_code="smoke_marker_remove_failed",
        continue_on_failure=True,
    )


def _run_smoke_marker_commands(
    context: StackContext,
    *,
    runner: CommandRunner,
    commands: Sequence[tuple[str, str]],
    success_code: str,
    failure_code: str,
    continue_on_failure: bool = False,
) -> None:
    has_failed = False
    for service, script in commands:
        shell = "/bin/bash" if service == "qdrant" else "/bin/sh"
        arguments = (
            *compose_arguments(context),
            "exec",
            "--no-TTY",
            service,
            shell,
            "-ec",
            script,
        )
        try:
            result = runner(
                arguments,
                timeout_seconds=_SMOKE_MARKER_TIMEOUT_SECONDS,
                environment=_command_environment(context),
            )
            is_success = result.return_code == 0 and result.stdout.strip() == success_code
            del result
        except Exception:
            is_success = False
        if not is_success:
            has_failed = True
            if not continue_on_failure:
                break
    if has_failed:
        raise StackFailure(StackExitCode.READINESS, failure_code)


def _validate_smoke_markers(markers: SmokeMarkerSet) -> None:
    if not re.fullmatch(r"[0-9a-f]{24}", markers.marker_key):
        raise StackFailure(StackExitCode.CONTRACT, "smoke_marker_invalid")
    if markers.qdrant_collection != f"stack_smoke_marker_{markers.marker_key}":
        raise StackFailure(StackExitCode.CONTRACT, "smoke_marker_invalid")
    if markers.qdrant_point_id != 1:
        raise StackFailure(StackExitCode.CONTRACT, "smoke_marker_invalid")
    if markers.redis_key != f"stack:smoke:{markers.marker_key}":
        raise StackFailure(StackExitCode.CONTRACT, "smoke_marker_invalid")


def _smoke_marker_commands(
    markers: SmokeMarkerSet,
    *,
    stage: str,
) -> tuple[tuple[str, str], ...]:
    scripts = {
        "create": (
            _postgresql_smoke_create_script(markers),
            _qdrant_smoke_create_script(markers),
            _neo4j_smoke_create_script(markers),
            _redis_smoke_create_script(markers),
        ),
        "verify": (
            _postgresql_smoke_verify_script(markers),
            _qdrant_smoke_verify_script(markers),
            _neo4j_smoke_verify_script(markers),
            _redis_smoke_verify_script(markers),
        ),
        "remove": (
            _postgresql_smoke_remove_script(markers),
            _qdrant_smoke_remove_script(markers),
            _neo4j_smoke_remove_script(markers),
            _redis_smoke_remove_script(markers),
        ),
    }
    selected_scripts = scripts.get(stage)
    if selected_scripts is None:
        raise StackFailure(StackExitCode.INTERNAL, "smoke_marker_stage_invalid")
    return tuple(zip(("postgresql", "qdrant", "neo4j", "redis"), selected_scripts, strict=True))


def _postgresql_smoke_create_script(markers: SmokeMarkerSet) -> str:
    return "\n".join(
        (
            "application_password=$(cat /run/secrets/postgres_application_password) || exit 75",
            '[ -n "$application_password" ] || exit 65',
            'sql="CREATE TABLE IF NOT EXISTS public.stack_smoke_marker '
            "(marker_key text PRIMARY KEY, marker_value text NOT NULL); "
            "ALTER TABLE public.stack_smoke_marker OWNER TO knowledge_app; "
            "INSERT INTO public.stack_smoke_marker(marker_key, marker_value) "
            f"VALUES ('{markers.marker_key}', 'ready') ON CONFLICT (marker_key) "
            'DO UPDATE SET marker_value = EXCLUDED.marker_value;"',
            'PGPASSWORD="$application_password" psql -XAtq --host 127.0.0.1 --port 5432 '
            "--username knowledge_app --dbname knowledge --set ON_ERROR_STOP=1 "
            '--command "$sql" >/dev/null 2>&1 || exit 75',
            "unset application_password sql",
            "printf '%s\\n' smoke_markers_created",
        )
    )


def _postgresql_smoke_verify_script(markers: SmokeMarkerSet) -> str:
    return "\n".join(
        (
            "application_password=$(cat /run/secrets/postgres_application_password) || exit 75",
            '[ -n "$application_password" ] || exit 65',
            'marker_count=$(PGPASSWORD="$application_password" psql -XAtq '
            "--host 127.0.0.1 --port 5432 --username knowledge_app --dbname knowledge "
            '--set ON_ERROR_STOP=1 --command "SELECT count(*) FROM '
            "public.stack_smoke_marker WHERE "
            f"marker_key = '{markers.marker_key}' AND marker_value = 'ready'\" "
            "2>/dev/null) || exit 75",
            '[ "$marker_count" = 1 ] || exit 65',
            'table_owner=$(PGPASSWORD="$application_password" psql -XAtq '
            "--host 127.0.0.1 --port 5432 --username knowledge_app --dbname knowledge "
            '--set ON_ERROR_STOP=1 --command "SELECT tableowner FROM pg_tables WHERE '
            "schemaname = 'public' AND tablename = 'stack_smoke_marker'\" "
            "2>/dev/null) || exit 75",
            '[ "$table_owner" = knowledge_app ] || exit 65',
            "unset application_password marker_count table_owner",
            "printf '%s\\n' smoke_markers_verified",
        )
    )


def _postgresql_smoke_remove_script(markers: SmokeMarkerSet) -> str:
    return "\n".join(
        (
            "application_password=$(cat /run/secrets/postgres_application_password) || exit 75",
            '[ -n "$application_password" ] || exit 65',
            'PGPASSWORD="$application_password" psql -XAtq --host 127.0.0.1 --port 5432 '
            "--username knowledge_app --dbname knowledge --set ON_ERROR_STOP=1 "
            '--command "DROP TABLE IF EXISTS public.stack_smoke_marker;" '
            ">/dev/null 2>&1 || exit 75",
            "unset application_password",
            "printf '%s\\n' smoke_markers_removed",
        )
    )


def _qdrant_smoke_create_script(markers: SmokeMarkerSet) -> str:
    return _qdrant_smoke_script(
        (
            (
                "PUT",
                f"/collections/{markers.qdrant_collection}",
                '{"vectors":{"size":4,"distance":"Cosine"}}',
            ),
            (
                "PUT",
                f"/collections/{markers.qdrant_collection}/points?wait=true",
                f'{{"points":[{{"id":{markers.qdrant_point_id},"vector":[1,0,0,0]}}]}}',
            ),
        ),
        success_code="smoke_markers_created",
    )


def _qdrant_smoke_verify_script(markers: SmokeMarkerSet) -> str:
    return _qdrant_smoke_script(
        (
            (
                "GET",
                f"/collections/{markers.qdrant_collection}/points/{markers.qdrant_point_id}",
                "",
            ),
        ),
        success_code="smoke_markers_verified",
    )


def _qdrant_smoke_remove_script(markers: SmokeMarkerSet) -> str:
    return _qdrant_smoke_script(
        (("DELETE", f"/collections/{markers.qdrant_collection}", ""),),
        success_code="smoke_markers_removed",
        expected_statuses=(200, 404),
    )


def _qdrant_smoke_script(
    requests: Sequence[tuple[str, str, str]],
    *,
    success_code: str,
    expected_statuses: tuple[int, ...] = (200,),
) -> str:
    status_pattern = "|".join(str(status) for status in expected_statuses)
    lines = [
        "api_key=$(sed -n 's/^  api_key: //p' /run/secrets/qdrant_config.yaml) || exit 75",
        '[ -n "$api_key" ] || exit 65',
        "request() {",
        "  method=$1",
        "  path=$2",
        "  body=$3",
        "  exec 3<>/dev/tcp/127.0.0.1/6333 || return 75",
        "  printf '%s %s HTTP/1.1\\r\\nHost: localhost\\r\\napi-key: %s\\r\\n"
        "Content-Type: application/json\\r\\nContent-Length: %s\\r\\n"
        'Connection: close\\r\\n\\r\\n%s\' "$method" "$path" "$api_key" '
        '"${#body}" "$body" >&3',
        "  IFS=' ' read -r _ status _ <&3 || return 75",
        "  exec 3<&- 3>&-",
        f'  case "$status" in {status_pattern}) ;; *) return 75 ;; esac',
        "}",
    ]
    lines.extend(
        f"request '{method}' '{path}' '{body}' || exit 75" for method, path, body in requests
    )
    lines.extend(("unset api_key", f"printf '%s\\n' {success_code}"))
    return "\n".join(lines)


def _neo4j_smoke_create_script(markers: SmokeMarkerSet) -> str:
    return _neo4j_smoke_script(
        "MERGE (marker:StackSmokeMarker "
        f"{{marker_key: '{markers.marker_key}'}}) SET marker.marker_value = 'ready' "
        "RETURN count(marker) AS marker_count",
        expected_scalar="1",
        success_code="smoke_markers_created",
    )


def _neo4j_smoke_verify_script(markers: SmokeMarkerSet) -> str:
    return _neo4j_smoke_script(
        "MATCH (marker:StackSmokeMarker "
        f"{{marker_key: '{markers.marker_key}', marker_value: 'ready'}}) "
        "RETURN count(marker) AS marker_count",
        expected_scalar="1",
        success_code="smoke_markers_verified",
    )


def _neo4j_smoke_remove_script(markers: SmokeMarkerSet) -> str:
    return _neo4j_smoke_script(
        "MATCH (marker:StackSmokeMarker "
        f"{{marker_key: '{markers.marker_key}'}}) WITH collect(marker) AS markers "
        "FOREACH (marker IN markers | DELETE marker) RETURN size(markers) AS marker_count",
        expected_scalars=("0", "1"),
        success_code="smoke_markers_removed",
    )


def _neo4j_smoke_script(
    query: str,
    *,
    expected_scalar: str | None = None,
    expected_scalars: tuple[str, ...] = (),
    success_code: str,
) -> str:
    scalar_pattern = "|".join(expected_scalars or ((expected_scalar,) if expected_scalar else ()))
    return "\n".join(
        (
            "neo4j_auth=$(cat /run/secrets/neo4j_auth) || exit 75",
            "neo4j_password=${neo4j_auth#neo4j/}",
            "result=$(cypher-shell --address bolt://127.0.0.1:7687 --username neo4j "
            f'--password "$neo4j_password" --format plain "{query}" 2>/dev/null) '
            "|| exit 75",
            f"printf '%s\\n' \"$result\" | tr -d '\\r' | grep -Eq '^({scalar_pattern})$' "
            "|| exit 65",
            "unset neo4j_auth neo4j_password result",
            f"printf '%s\\n' {success_code}",
        )
    )


def _redis_smoke_create_script(markers: SmokeMarkerSet) -> str:
    return _redis_smoke_script(
        f'SET "{markers.redis_key}" ready',
        expected_scalar="OK",
        success_code="smoke_markers_created",
    )


def _redis_smoke_verify_script(markers: SmokeMarkerSet) -> str:
    return _redis_smoke_script(
        f'GET "{markers.redis_key}"',
        expected_scalar="ready",
        success_code="smoke_markers_verified",
    )


def _redis_smoke_remove_script(markers: SmokeMarkerSet) -> str:
    return _redis_smoke_script(
        f'DEL "{markers.redis_key}"',
        expected_scalars=("0", "1"),
        success_code="smoke_markers_removed",
    )


def _redis_smoke_script(
    command: str,
    *,
    expected_scalar: str | None = None,
    expected_scalars: tuple[str, ...] = (),
    success_code: str,
) -> str:
    scalar_pattern = "|".join(expected_scalars or ((expected_scalar,) if expected_scalar else ()))
    return "\n".join(
        (
            "redis_password=$(sed -n 's/^user knowledge on >\\([^ ]*\\).*/\\1/p' "
            "/run/secrets/redis_acl) || exit 75",
            '[ -n "$redis_password" ] || exit 65',
            "result=$(redis-cli --raw --no-auth-warning --user knowledge "
            f'--pass "$redis_password" {command} 2>/dev/null) || exit 75',
            f'case "$result" in {scalar_pattern}) ;; *) exit 65 ;; esac',
            "unset redis_password result",
            f"printf '%s\\n' {success_code}",
        )
    )


def _stop_smoke_redis(
    context: StackContext,
    *,
    runner: CommandRunner,
) -> None:
    result = _run_lifecycle_command(
        runner,
        [*compose_arguments(context), "stop", "--timeout", "15", "redis"],
        timeout_seconds=_SMOKE_REDIS_OPERATION_TIMEOUT_SECONDS,
        context=context,
    )
    if result.return_code != 0:
        raise StackFailure(StackExitCode.STARTUP, "smoke_redis_stop_failed")


def _verify_smoke_redis_outage(
    context: StackContext,
    *,
    runner: CommandRunner,
) -> None:
    try:
        verify_stack(context, runner=runner)
    except StackFailure as failure:
        if (
            failure.exit_code is StackExitCode.READINESS
            and failure.result_code == "redis_contract_failed"
        ):
            return
    raise StackFailure(StackExitCode.READINESS, "smoke_redis_outage_not_detected")


def _start_smoke_redis(
    context: StackContext,
    *,
    runner: CommandRunner,
) -> None:
    result = _run_lifecycle_command(
        runner,
        [*compose_arguments(context), "start", "redis"],
        timeout_seconds=_SMOKE_REDIS_OPERATION_TIMEOUT_SECONDS,
        context=context,
    )
    if result.return_code != 0:
        raise StackFailure(StackExitCode.STARTUP, "smoke_redis_start_failed")
    wait_for_stack(
        context,
        runner=runner,
        deadline_seconds=_SMOKE_REDIS_RECOVERY_DEADLINE_SECONDS,
    )


def _assert_smoke_project_absent(
    context: StackContext,
    *,
    runner: CommandRunner,
) -> None:
    project_filter = f"label=com.docker.compose.project={context.project_name}"
    inventory_commands = (
        ("docker", "container", "ls", "--all", "--quiet", "--filter", project_filter),
        ("docker", "network", "ls", "--quiet", "--filter", project_filter),
        ("docker", "volume", "ls", "--quiet", "--filter", project_filter),
    )
    for command in inventory_commands:
        result = _run_lifecycle_command(
            runner,
            command,
            timeout_seconds=_STACK_STATUS_TIMEOUT_SECONDS,
            context=context,
        )
        has_resources = bool(result.stdout.strip())
        return_code = result.return_code
        del result
        if return_code != 0:
            raise StackFailure(StackExitCode.STARTUP, "smoke_cleanup_inventory_failed")
        if has_resources:
            raise StackFailure(StackExitCode.STARTUP, "smoke_cleanup_incomplete")


def _smoke_secret_fingerprint(
    paths: StackPaths,
    application_secret_references: _ApplicationSecretReferences,
) -> str:
    try:
        secret_state = inspect_secret_set(
            paths,
            application_secret_references=application_secret_references,
        )
    except StackFailure:
        raise StackFailure(StackExitCode.CONTRACT, "smoke_secret_set_changed") from None
    if secret_state is not SecretSetState.COMPLETE:
        raise StackFailure(StackExitCode.CONTRACT, "smoke_secret_set_changed")
    digest = hashlib.sha256()
    try:
        for filename in sorted(_SECRET_FILENAMES):
            digest.update(filename.encode("ascii"))
            digest.update(b"\0")
            digest.update((paths.secret_directory / filename).read_bytes())
            digest.update(b"\0")
    except OSError:
        raise StackFailure(StackExitCode.CONTRACT, "smoke_secret_set_changed") from None
    return digest.hexdigest()


class _DefaultSmokeOperations:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner
        self._secret_fingerprint: str | None = None
        self._startup_count = 0

    def reset_before(self, context: StackContext) -> None:
        reset_stack(context, confirm_project=context.project_name, runner=self._runner)

    def bootstrap(self, context: StackContext) -> None:
        bootstrap_secret_set(
            context.paths,
            application_secret_references=context.application_secret_references,
        )
        self._secret_fingerprint = _smoke_secret_fingerprint(
            context.paths,
            context.application_secret_references,
        )

    def config(self, context: StackContext) -> None:
        validate_compose_config(context, runner=self._runner)

    def up(self, context: StackContext) -> None:
        self._startup_count += 1
        if self._startup_count <= 2:
            stack_up(context, runner=self._runner)
            return
        if self._startup_count == 3:
            _repeat_smoke_stack_up(context, runner=self._runner)
            return
        raise StackFailure(StackExitCode.INTERNAL, "smoke_startup_count_invalid")

    def verify(self, context: StackContext) -> None:
        verify_stack(context, runner=self._runner)

    def new_markers(self, context: StackContext) -> SmokeMarkerSet:
        del context
        return _new_smoke_markers()

    def create_markers(self, context: StackContext, markers: SmokeMarkerSet) -> None:
        _create_smoke_markers(context, runner=self._runner, markers=markers)

    def down_preserve(self, context: StackContext) -> None:
        stack_down(context, runner=self._runner)

    def verify_markers(self, context: StackContext, markers: SmokeMarkerSet) -> None:
        _verify_smoke_markers(context, markers, runner=self._runner)

    def stop_redis(self, context: StackContext) -> None:
        _stop_smoke_redis(context, runner=self._runner)

    def verify_outage(self, context: StackContext) -> None:
        _verify_smoke_redis_outage(context, runner=self._runner)

    def start_redis(self, context: StackContext) -> None:
        _start_smoke_redis(context, runner=self._runner)

    def verify_recovery(self, context: StackContext) -> None:
        verify_stack(context, runner=self._runner)

    def ensure_redis_recovered(self, context: StackContext) -> None:
        with suppress(StackFailure):
            _start_smoke_redis(context, runner=self._runner)
        verify_stack(context, runner=self._runner)

    def remove_markers(self, context: StackContext, markers: SmokeMarkerSet) -> None:
        _remove_smoke_markers(context, markers, runner=self._runner)

    def reset_after(self, context: StackContext) -> None:
        reset_stack(context, confirm_project=context.project_name, runner=self._runner)
        _assert_smoke_project_absent(context, runner=self._runner)
        if (
            self._secret_fingerprint is not None
            and _smoke_secret_fingerprint(
                context.paths,
                context.application_secret_references,
            )
            != self._secret_fingerprint
        ):
            raise StackFailure(StackExitCode.CONTRACT, "smoke_secret_set_changed")


def _run_semantic_probes(
    context: StackContext,
    *,
    runner: CommandRunner,
    deadline_monotonic: float,
    clock: Callable[[], float],
) -> tuple[ProbeResult, ...]:
    probe_results: list[ProbeResult] = []
    for service in _PROBE_SERVICES:
        started_monotonic = clock()
        timeout_seconds = min(
            _SEMANTIC_PROBE_TIMEOUT_SECONDS,
            _remaining_seconds(deadline_monotonic, clock),
        )
        arguments, success_code = _semantic_probe_arguments(context, service)
        try:
            raw_result = runner(
                arguments,
                timeout_seconds=timeout_seconds,
                environment=_command_environment(context),
            )
        except StackFailure as failure:
            exit_code = (
                StackExitCode.CONTRACT
                if failure.exit_code is StackExitCode.CONTRACT
                else StackExitCode.READINESS
            )
            raise StackFailure(exit_code, _probe_failure_code(service)) from None
        except Exception:
            raise StackFailure(
                StackExitCode.READINESS,
                _probe_failure_code(service),
            ) from None
        return_code = raw_result.return_code
        has_success_marker = raw_result.stdout.strip() == success_code
        del raw_result
        if return_code != 0:
            exit_code = StackExitCode.CONTRACT if return_code == 65 else StackExitCode.READINESS
            raise StackFailure(exit_code, _probe_failure_code(service))
        if not has_success_marker:
            raise StackFailure(StackExitCode.CONTRACT, _probe_failure_code(service))
        latency_ms = max(0, int((clock() - started_monotonic) * 1000))
        probe_results.append(ProbeResult(service, True, success_code, latency_ms))
    return tuple(probe_results)


def _semantic_probe_arguments(context: StackContext, service: str) -> tuple[tuple[str, ...], str]:
    success_code = f"{service.replace('-', '_')}_contract_ready"
    if service == "temporal-ui":
        probe_script = (
            "import sys, urllib.error, urllib.request\n"
            "try:\n"
            "    response = urllib.request.urlopen(sys.argv[2], timeout=8)\n"
            "except urllib.error.HTTPError as error:\n"
            "    error.close()\n"
            "    sys.exit(65)\n"
            "except (OSError, urllib.error.URLError):\n"
            "    sys.exit(75)\n"
            "status = response.status\n"
            "response.close()\n"
            "sys.stdout.write(sys.argv[3] + '\\n') if status == 200 else sys.exit(65)"
        )
        return (
            sys.executable,
            "-c",
            probe_script,
            "temporal-ui",
            f"http://127.0.0.1:{context.ports['TEMPORAL_UI_PORT']}/healthz",
            success_code,
        ), success_code

    scripts = {
        "postgresql": _postgresql_probe_script(success_code),
        "qdrant": _qdrant_probe_script(success_code),
        "neo4j": _neo4j_probe_script(success_code),
        "redis": _redis_probe_script(success_code),
        "temporal": _temporal_probe_script(success_code),
    }
    script = scripts.get(service)
    if script is None:
        raise StackFailure(StackExitCode.INTERNAL, "probe_invariant_failed")
    shell = "/bin/bash" if service == "qdrant" else "/bin/sh"
    target_service = "temporal-cli" if service == "temporal" else service
    return (
        *compose_arguments(context),
        "exec",
        "--no-TTY",
        target_service,
        shell,
        "-ec",
        script,
    ), success_code


def _probe_failure_code(service: str) -> str:
    return f"{service.replace('-', '_')}_contract_failed"


def _postgresql_probe_script(success_code: str) -> str:
    return "\n".join(
        (
            "admin_password=$(cat /run/secrets/postgres_admin_password) || exit 75",
            "application_password=$(cat /run/secrets/postgres_application_password) || exit 75",
            "temporal_password=$(cat /run/secrets/postgres_temporal_password) || exit 75",
            '[ -n "$admin_password" ] && [ -n "$application_password" ] '
            '&& [ -n "$temporal_password" ] || exit 65',
            'contract=$(PGPASSWORD="$admin_password" psql -XAtq --host 127.0.0.1 '
            "--port 5432 --username stack_admin --dbname postgres --set ON_ERROR_STOP=1 "
            "--command \"SELECT (current_user = 'stack_admin' "
            "AND has_database_privilege('knowledge_app', 'knowledge', 'CONNECT') "
            "AND NOT has_database_privilege('knowledge_app', 'temporal', 'CONNECT') "
            "AND NOT has_database_privilege('knowledge_app', 'temporal_visibility', 'CONNECT') "
            "AND has_database_privilege('temporal_service', 'temporal', 'CONNECT') "
            "AND has_database_privilege('temporal_service', 'temporal_visibility', 'CONNECT') "
            "AND NOT has_database_privilege('temporal_service', 'knowledge', 'CONNECT') "
            "AND (SELECT count(*) = 3 FROM pg_database WHERE "
            "(datname, pg_get_userbyid(datdba)) IN (('knowledge', 'knowledge_app'), "
            "('temporal', 'temporal_service'), "
            "('temporal_visibility', 'temporal_service'))))::int\" 2>/dev/null) || exit 75",
            '[ "$contract" = 1 ] || exit 65',
            'application_identity=$(PGPASSWORD="$application_password" psql -XAtq '
            "--host 127.0.0.1 --port 5432 --username knowledge_app --dbname knowledge "
            "--set ON_ERROR_STOP=1 --command \"SELECT (current_user = 'knowledge_app')::int\" "
            "2>/dev/null) || exit 75",
            '[ "$application_identity" = 1 ] || exit 65',
            'temporal_identity=$(PGPASSWORD="$temporal_password" psql -XAtq '
            "--host 127.0.0.1 --port 5432 --username temporal_service --dbname temporal "
            "--set ON_ERROR_STOP=1 --command \"SELECT (current_user = 'temporal_service')::int\" "
            "2>/dev/null) || exit 75",
            '[ "$temporal_identity" = 1 ] || exit 65',
            'temporal_visibility_identity=$(PGPASSWORD="$temporal_password" psql -XAtq '
            "--host 127.0.0.1 --port 5432 --username temporal_service "
            "--dbname temporal_visibility --set ON_ERROR_STOP=1 "
            "--command \"SELECT (current_user = 'temporal_service')::int\" 2>/dev/null) "
            "|| exit 75",
            '[ "$temporal_visibility_identity" = 1 ] || exit 65',
            'if PGPASSWORD="$application_password" psql -XAtq --host 127.0.0.1 '
            "--port 5432 --username knowledge_app --dbname temporal --command 'SELECT 1' "
            ">/dev/null 2>&1; then exit 65; fi",
            'if PGPASSWORD="$application_password" psql -XAtq --host 127.0.0.1 '
            "--port 5432 --username knowledge_app --dbname temporal_visibility "
            "--command 'SELECT 1' >/dev/null 2>&1; then exit 65; fi",
            'if PGPASSWORD="$temporal_password" psql -XAtq --host 127.0.0.1 '
            "--port 5432 --username temporal_service --dbname knowledge --command 'SELECT 1' "
            ">/dev/null 2>&1; then exit 65; fi",
            "unset admin_password application_password temporal_password",
            "unset contract application_identity temporal_identity temporal_visibility_identity",
            f"printf '%s\\n' {success_code}",
        )
    )


def _qdrant_probe_script(success_code: str) -> str:
    return "\n".join(
        (
            "api_key=$(sed -n 's/^  api_key: //p' /run/secrets/qdrant_config.yaml) || exit 75",
            '[ -n "$api_key" ] || exit 65',
            "http_status() {",
            "  api_key_header=$1",
            "  exec 3<>/dev/tcp/127.0.0.1/6333 || return 75",
            '  if [ -n "$api_key_header" ]; then',
            "    printf 'GET /collections HTTP/1.1\\r\\nHost: localhost\\r\\n"
            'api-key: %s\\r\\nConnection: close\\r\\n\\r\\n\' "$api_key_header" >&3',
            "  else",
            "    printf 'GET /collections HTTP/1.1\\r\\nHost: localhost\\r\\n"
            "Connection: close\\r\\n\\r\\n' >&3",
            "  fi",
            "  IFS=' ' read -r _ status _ <&3 || return 75",
            "  exec 3<&- 3>&-",
            "  printf '%s' \"$status\"",
            "}",
            'authenticated=$(http_status "$api_key") || exit 75',
            "unauthenticated=$(http_status '') || exit 75",
            "unset api_key",
            '[ "$authenticated" = 200 ] || exit 75',
            'case "$unauthenticated" in 401|403) ;; *) exit 65 ;; esac',
            f"printf '%s\\n' {success_code}",
        )
    )


def _neo4j_probe_script(success_code: str) -> str:
    return "\n".join(
        (
            "neo4j_auth=$(cat /run/secrets/neo4j_auth) || exit 75",
            "neo4j_password=${neo4j_auth#neo4j/}",
            "scalar=$(cypher-shell --address bolt://127.0.0.1:7687 --username neo4j "
            '--password "$neo4j_password" --format plain '
            "'RETURN 1 AS scalar') || exit 75",
            "plugins=$(cypher-shell --address bolt://127.0.0.1:7687 --username neo4j "
            '--password "$neo4j_password" --format plain '
            "\"SHOW PROCEDURES YIELD name WHERE name STARTS WITH 'apoc.' "
            "OR name STARTS WITH 'gds.' RETURN count(name) AS enabled_plugins\") || exit 75",
            "if cypher-shell --address bolt://127.0.0.1:7687 --username neo4j "
            "--password invalid-probe-credential --format plain 'RETURN 1' "
            ">/dev/null 2>&1; then exit 65; fi",
            "unset neo4j_auth neo4j_password",
            "[ \"$(printf '%s\\n' \"$scalar\" | tail -n 1 | tr -d '\\r')\" = 1 ] || exit 65",
            "[ \"$(printf '%s\\n' \"$plugins\" | tail -n 1 | tr -d '\\r')\" = 0 ] || exit 65",
            f"printf '%s\\n' {success_code}",
        )
    )


def _redis_probe_script(success_code: str) -> str:
    return "\n".join(
        (
            "redis_password=$(sed -n 's/^user knowledge on >\\([^ ]*\\).*/\\1/p' "
            "/run/secrets/redis_acl) || exit 75",
            '[ -n "$redis_password" ] || exit 65',
            "probe_key=__knowledge_stack_verify__$$",
            'trap \'redis-cli --no-auth-warning --user knowledge --pass "$redis_password" '
            'DEL "$probe_key" >/dev/null 2>&1 || true\' EXIT HUP INT TERM',
            '[ "$(redis-cli --raw --no-auth-warning --user knowledge '
            '--pass "$redis_password" PING)" = PONG ] || exit 75',
            '[ "$(redis-cli --raw --no-auth-warning --user knowledge '
            '--pass "$redis_password" SET "$probe_key" ready)" = OK ] || exit 75',
            '[ "$(redis-cli --raw --no-auth-warning --user knowledge '
            '--pass "$redis_password" GET "$probe_key")" = ready ] || exit 75',
            "unauthenticated=$(redis-cli --raw PING 2>/dev/null || true)",
            '[ "$unauthenticated" != PONG ] || exit 65',
            '[ "$(redis-cli --raw --no-auth-warning --user knowledge '
            '--pass "$redis_password" CONFIG GET appendonly | tail -n 1)" = yes ] || exit 65',
            '[ "$(redis-cli --raw --no-auth-warning --user knowledge '
            '--pass "$redis_password" CONFIG GET maxmemory | tail -n 1)" = 134217728 ] || exit 65',
            '[ "$(redis-cli --raw --no-auth-warning --user knowledge '
            '--pass "$redis_password" CONFIG GET maxmemory-policy | tail -n 1)" '
            "= noeviction ] || exit 65",
            f"printf '%s\\n' {success_code}",
        )
    )


def _temporal_probe_script(success_code: str) -> str:
    return "\n".join(
        (
            "temporal --address temporal:7233 --namespace knowledge "
            "--client-connect-timeout 5s --command-timeout 8s --color never "
            "--disable-config-file operator cluster health >/dev/null 2>&1 || exit 75",
            "description=$(temporal --address temporal:7233 --namespace knowledge "
            "--client-connect-timeout 5s --command-timeout 8s --color never "
            "--disable-config-file --output json operator namespace describe) || exit 75",
            "compact=$(printf '%s' \"$description\" | tr -d '\\r\\n ')",
            "unset description",
            "printf '%s' \"$compact\" | grep -Eq "
            '\'"state":"(NAMESPACE_STATE_)?REGISTERED"\' || exit 65',
            "printf '%s' \"$compact\" | grep -Eq "
            '\'"(retention|workflowExecutionRetentionTtl|workflowExecutionRetentionPeriod)":'
            '("(7d|168h|168h0m|168h0m0s|604800|604800s)"|'
            '\\{"seconds":"?604800"?)\' || exit 65',
            f"printf '%s\\n' {success_code}",
        )
    )


def _validate_lifecycle_project(context: StackContext) -> None:
    validate_project_name(context.project_name)
    if context.project_name.startswith("knowledge-ci-") and context.environment.get("CI") != "true":
        raise StackFailure(StackExitCode.CLI, "ci_project_requires_ci")


def _command_environment(context: StackContext) -> dict[str, str]:
    environment = sanitize_subprocess_environment(context.environment)
    environment.update(
        {
            variable: str(context.ports[variable])
            for variable in _PORT_VARIABLES
            if variable in context.ports
        }
    )
    return environment


def _run_prerequisite_command(
    runner: CommandRunner,
    arguments: Sequence[str],
    context: StackContext,
    *,
    result_code: str,
) -> CommandResult:
    try:
        result = runner(
            arguments,
            timeout_seconds=_PREREQUISITE_TIMEOUT_SECONDS,
            environment=_command_environment(context),
        )
    except StackFailure as failure:
        if failure.exit_code is StackExitCode.CLI:
            raise
        raise StackFailure(StackExitCode.PREREQUISITE, result_code) from None
    except Exception:
        raise StackFailure(StackExitCode.PREREQUISITE, result_code) from None
    if result.return_code != 0:
        raise StackFailure(StackExitCode.PREREQUISITE, result_code)
    return result


def _parse_compose_version(
    raw_version: str,
) -> tuple[tuple[int, int, int], tuple[str, ...] | None]:
    matched = _COMPOSE_VERSION_PATTERN.fullmatch(raw_version.strip())
    if matched is None:
        raise StackFailure(StackExitCode.PREREQUISITE, "compose_version_invalid")
    major, minor, patch, raw_prerelease = matched.groups()
    compose_version = (int(major), int(minor), int(patch))
    compose_prerelease = tuple(raw_prerelease.split(".")) if raw_prerelease is not None else None
    return compose_version, compose_prerelease


def _require_complete_secret_set(
    context: StackContext,
    *,
    runner: CommandRunner,
    inspect_project_volumes: bool,
) -> None:
    def list_project_volumes() -> Sequence[str]:
        if inspect_project_volumes:
            return _list_project_volumes(context, runner)
        return ()

    state = validate_secret_set(
        context.paths,
        list_project_volumes=list_project_volumes,
        application_secret_references=context.application_secret_references,
    )
    if state is not SecretSetState.COMPLETE:
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_missing")


def _list_project_volumes(context: StackContext, runner: CommandRunner) -> tuple[str, ...]:
    result = _run_prerequisite_command(
        runner,
        (
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={context.project_name}",
        ),
        context,
        result_code="volume_inspection_failed",
    )
    return tuple(line for line in result.stdout.splitlines() if line)


def _inspect_project_volume_label(
    context: StackContext,
    runner: CommandRunner,
    volume_name: str,
) -> str:
    result = _run_prerequisite_command(
        runner,
        (
            "docker",
            "volume",
            "inspect",
            "--format",
            '{{index .Labels "com.docker.compose.project"}}\t'
            '{{index .Labels "com.docker.compose.volume"}}',
            volume_name,
        ),
        context,
        result_code="volume_inspection_failed",
    )
    label_rows = tuple(line for line in result.stdout.splitlines() if line)
    del result
    if len(label_rows) != 1:
        raise StackFailure(StackExitCode.CONTRACT, "project_volume_label_invalid")
    labels = label_rows[0].split("\t", maxsplit=1)
    if len(labels) != 2 or labels[0] != context.project_name or not labels[1]:
        raise StackFailure(StackExitCode.CONTRACT, "project_volume_label_invalid")
    return labels[1]


def _resolve_reset_volumes(
    context: StackContext,
    runner: CommandRunner,
) -> tuple[str, ...]:
    project_volumes = set(_list_project_volumes(context, runner))
    if not project_volumes:
        return ()
    volumes_by_label: dict[str, set[str]] = {label: set() for label in _RESET_VOLUME_LABELS}
    for volume_name in project_volumes:
        volume_label = _inspect_project_volume_label(context, runner, volume_name)
        if volume_label not in volumes_by_label:
            raise StackFailure(StackExitCode.CONTRACT, "unexpected_project_volume")
        volumes_by_label[volume_label].add(volume_name)
    if any(len(names) > 1 for names in volumes_by_label.values()):
        raise StackFailure(StackExitCode.CONTRACT, "project_volume_label_ambiguous")
    resolved_volumes = set().union(*volumes_by_label.values())
    if (
        any(len(names) != 1 for names in volumes_by_label.values())
        or resolved_volumes != project_volumes
        or len(resolved_volumes) != len(_RESET_VOLUME_LABELS)
    ):
        raise StackFailure(StackExitCode.CONTRACT, "project_volume_set_incomplete")
    return tuple(sorted(resolved_volumes))


def _run_compose_config(context: StackContext, runner: CommandRunner) -> None:
    result = _run_lifecycle_command(
        runner,
        [*compose_arguments(context), "config", "--quiet"],
        timeout_seconds=_COMPOSE_CONFIG_TIMEOUT_SECONDS,
        context=context,
    )
    if result.return_code != 0:
        raise StackFailure(StackExitCode.CONTRACT, "compose_config_invalid")


def _run_lifecycle_command(
    runner: CommandRunner,
    arguments: Sequence[str],
    *,
    timeout_seconds: float,
    context: StackContext,
) -> CommandResult:
    try:
        return runner(
            arguments,
            timeout_seconds=timeout_seconds,
            environment=_command_environment(context),
        )
    except StackFailure:
        raise
    except Exception:
        raise StackFailure(StackExitCode.INTERNAL, "lifecycle_internal_error") from None


def _wait_for_stack_until(
    context: StackContext,
    *,
    runner: CommandRunner,
    deadline_monotonic: float,
    poll_interval_seconds: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict[str, object]:
    while True:
        remaining_seconds = _remaining_seconds(deadline_monotonic, clock)
        status = _read_stack_status(
            context,
            runner=runner,
            timeout_seconds=min(_STACK_STATUS_TIMEOUT_SECONDS, remaining_seconds),
        )
        if _status_has_failed_initializer(status):
            raise StackFailure(StackExitCode.STARTUP, "initializer_failed")
        state = status["state"]
        if state == "ready":
            return status
        if state in {"degraded", "stopped"}:
            raise StackFailure(StackExitCode.READINESS, "stack_readiness_failed")
        remaining_after_status = _remaining_seconds(deadline_monotonic, clock)
        sleep(min(poll_interval_seconds, remaining_after_status))


def _read_startup_failure_status(
    context: StackContext,
    *,
    runner: CommandRunner,
    deadline_monotonic: float,
) -> dict[str, object]:
    """Return the existing safe status shape or a closed unavailable token after failed startup."""
    try:
        return _read_stack_status(
            context,
            runner=runner,
            timeout_seconds=min(
                _STACK_STATUS_TIMEOUT_SECONDS,
                _remaining_seconds(deadline_monotonic, time.monotonic),
            ),
        )
    except StackFailure:
        return {"result_code": "stack_status_unavailable", "state": "error"}


def _remaining_seconds(deadline_monotonic: float, clock: Callable[[], float]) -> float:
    remaining_seconds = deadline_monotonic - clock()
    if remaining_seconds <= 0:
        raise StackFailure(StackExitCode.READINESS, "stack_readiness_timeout")
    return remaining_seconds


def _validate_deadline(deadline_seconds: float) -> None:
    if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
        raise StackFailure(StackExitCode.CLI, "invalid_deadline")


def _read_stack_status(
    context: StackContext,
    *,
    runner: CommandRunner,
    timeout_seconds: float,
) -> dict[str, object]:
    result = _run_lifecycle_command(
        runner,
        [*compose_arguments(context), "ps", "--all", "--format", _COMPOSE_PS_TEMPLATE],
        timeout_seconds=timeout_seconds,
        context=context,
    )
    if result.return_code != 0:
        raise StackFailure(StackExitCode.READINESS, "stack_status_unavailable")
    rows = _parse_compose_ps(result.stdout)
    services: dict[str, dict[str, object]] = {}
    initializers: dict[str, dict[str, object]] = {}
    for row in rows:
        service_name = row.get("Service")
        if not isinstance(service_name, str):
            raise StackFailure(StackExitCode.READINESS, "stack_status_invalid")
        state = _stable_container_state(row.get("State"))
        if service_name in _RUNTIME_SERVICE_NAMES:
            if service_name in services:
                raise StackFailure(StackExitCode.READINESS, "stack_status_invalid")
            services[service_name] = {
                "state": state,
                "health": _stable_health_state(row.get("Health")),
            }
        elif service_name in _INITIALIZER_SERVICE_NAMES:
            if service_name in initializers:
                raise StackFailure(StackExitCode.READINESS, "stack_status_invalid")
            initializers[service_name] = {
                "state": state,
                "exit_code": _stable_exit_code(row.get("ExitCode")),
            }
        else:
            raise StackFailure(StackExitCode.READINESS, "stack_status_invalid")

    sorted_services = {name: services[name] for name in sorted(services)}
    sorted_initializers = {name: initializers[name] for name in sorted(initializers)}
    aggregate_state = _aggregate_stack_state(sorted_services, sorted_initializers)
    return {
        "project": context.project_name,
        "state": aggregate_state,
        "services": sorted_services,
        "initializers": sorted_initializers,
        "result_code": f"stack_{aggregate_state}",
    }


def _parse_compose_ps(raw_status: str) -> list[dict[str, object]]:
    try:
        loaded: object = json.loads(raw_status)
    except json.JSONDecodeError, TypeError:
        loaded_rows: list[object] = []
        try:
            loaded_rows = [json.loads(line) for line in raw_status.splitlines() if line.strip()]
        except json.JSONDecodeError, TypeError:
            raise StackFailure(StackExitCode.READINESS, "stack_status_invalid") from None
        loaded = loaded_rows
    if isinstance(loaded, dict):
        loaded = [loaded]
    if not isinstance(loaded, list):
        raise StackFailure(StackExitCode.READINESS, "stack_status_invalid")
    rows: list[dict[str, object]] = []
    for raw_row in loaded:
        if not isinstance(raw_row, dict):
            raise StackFailure(StackExitCode.READINESS, "stack_status_invalid")
        rows.append(cast(dict[str, object], raw_row))
    return rows


def _stable_container_state(raw_state: object) -> str:
    if not isinstance(raw_state, str):
        return "unknown"
    normalized = raw_state.lower()
    return normalized if normalized in _STABLE_CONTAINER_STATES else "unknown"


def _stable_health_state(raw_health: object) -> str:
    if raw_health == "":
        return "none"
    if not isinstance(raw_health, str):
        return "unknown"
    normalized = raw_health.lower()
    return normalized if normalized in _STABLE_HEALTH_STATES else "unknown"


def _stable_exit_code(raw_exit_code: object) -> int | None:
    if type(raw_exit_code) is int and -255 <= raw_exit_code <= 255:
        return raw_exit_code
    return None


def _aggregate_stack_state(
    services: Mapping[str, Mapping[str, object]],
    initializers: Mapping[str, Mapping[str, object]],
) -> str:
    if not services and not initializers:
        return "absent"
    if _has_failed_initializer(initializers):
        return "degraded"
    if any(
        service["state"] in {"dead", "unknown"} or service["health"] in {"unhealthy", "unknown"}
        for service in services.values()
    ):
        return "degraded"
    is_ready = (
        set(services) == _RUNTIME_SERVICE_NAMES
        and set(initializers) == _INITIALIZER_SERVICE_NAMES
        and all(
            service["state"] == "running" and service["health"] == "healthy"
            for service in services.values()
        )
        and all(
            initializer["state"] == "exited" and initializer["exit_code"] == 0
            for initializer in initializers.values()
        )
    )
    if is_ready:
        return "ready"
    if services and all(service["state"] == "exited" for service in services.values()):
        return "stopped"
    if any(service["state"] == "exited" for service in services.values()):
        return "degraded"
    if any(
        service["state"] in {"created", "running", "restarting", "paused"}
        for service in (*services.values(), *initializers.values())
    ):
        return "starting"
    return "degraded"


def _has_failed_initializer(
    initializers: Mapping[str, Mapping[str, object]],
) -> bool:
    return any(
        initializer["state"] == "exited" and initializer["exit_code"] not in {None, 0}
        for initializer in initializers.values()
    )


def _status_has_failed_initializer(status: Mapping[str, object]) -> bool:
    initializers = status.get("initializers")
    if not isinstance(initializers, dict):
        raise StackFailure(StackExitCode.INTERNAL, "status_invariant_failed")
    return _has_failed_initializer(cast(dict[str, dict[str, object]], initializers))


def _resolve_beneath(repository_root: Path, *parts: str) -> Path:
    candidate = (repository_root.joinpath(*parts)).resolve(strict=False)
    is_beneath_repository = True
    try:
        candidate.relative_to(repository_root)
    except ValueError:
        is_beneath_repository = False
    if not is_beneath_repository:
        raise StackFailure(StackExitCode.INTERNAL, "invalid_stack_path")
    return candidate


def _validate_secret_directory_location(paths: StackPaths) -> Path:
    """Prove that callers cannot redirect secret writes outside the repository contract."""
    try:
        repository_root = paths.repository_root.resolve(strict=False)
    except OSError:
        raise StackFailure(StackExitCode.INTERNAL, "invalid_secret_directory") from None
    expected_directory = repository_root / ".local" / "stack-secrets"
    if paths.secret_directory != expected_directory:
        raise StackFailure(StackExitCode.INTERNAL, "invalid_secret_directory")
    try:
        resolved_secret_directory = expected_directory.resolve(strict=False)
        resolved_secret_directory.relative_to(repository_root)
    except OSError, ValueError:
        raise StackFailure(StackExitCode.INTERNAL, "invalid_secret_directory") from None
    if resolved_secret_directory != expected_directory:
        raise StackFailure(StackExitCode.INTERNAL, "invalid_secret_directory")
    return expected_directory


def _lstat_or_failure(path: Path, result_code: str) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise StackFailure(StackExitCode.CONTRACT, result_code) from None


def _validate_private_directory(
    path: Path, path_stat: os.stat_result, *, require_mode: bool = False
) -> None:
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise StackFailure(StackExitCode.CONTRACT, "unsafe_secret_set")
    _validate_path_owner(path, path_stat)
    if require_mode:
        _validate_private_mode(path_stat, 0o700)


def _validate_private_file(
    path: Path, path_stat: os.stat_result, *, require_mode: bool = False
) -> None:
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise StackFailure(StackExitCode.CONTRACT, "unsafe_secret_set")
    _validate_path_owner(path, path_stat)
    if require_mode:
        _validate_private_mode(path_stat, 0o600)


def _validate_path_owner(path: Path, path_stat: os.stat_result) -> None:
    if sys.platform == "win32":
        if not _is_current_windows_user_owner(path):
            raise StackFailure(StackExitCode.CONTRACT, "unsafe_secret_set")
        return
    if hasattr(os, "getuid"):
        if path_stat.st_uid != os.getuid():
            raise StackFailure(StackExitCode.CONTRACT, "unsafe_secret_set")
        return
    raise StackFailure(StackExitCode.CONTRACT, "unsafe_secret_set")


def _is_current_windows_user_owner(path: Path) -> bool:
    """Compare the path owner SID with the current process-token owner SID."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        token_query = 0x0008
        token_owner = 4
        error_insufficient_buffer = 122

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        advapi32.EqualSid.restype = wintypes.BOOL

        class TokenOwner(ctypes.Structure):
            _fields_ = [("owner", ctypes.c_void_p)]

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
        ):
            return False
        try:
            required_size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, token_owner, None, 0, ctypes.byref(required_size))
            if ctypes.get_last_error() != error_insufficient_buffer or not required_size.value:
                return False
            token_buffer = ctypes.create_string_buffer(required_size.value)
            if not advapi32.GetTokenInformation(
                token,
                token_owner,
                ctypes.cast(token_buffer, ctypes.c_void_p),
                required_size,
                ctypes.byref(required_size),
            ):
                return False
            token_owner_data = ctypes.cast(token_buffer, ctypes.POINTER(TokenOwner)).contents
            owner_sid = ctypes.c_void_p()
            security_descriptor = ctypes.c_void_p()
            result = advapi32.GetNamedSecurityInfoW(
                str(path),
                1,
                1,
                ctypes.byref(owner_sid),
                None,
                None,
                None,
                ctypes.byref(security_descriptor),
            )
            if result != 0 or not owner_sid.value:
                return False
            try:
                return bool(advapi32.EqualSid(owner_sid, token_owner_data.owner))
            finally:
                if security_descriptor.value:
                    kernel32.LocalFree(security_descriptor)
        finally:
            kernel32.CloseHandle(token)
    except AttributeError, OSError, TypeError:
        return False


def _validate_complete_secret_contents(secret_directory: Path) -> None:
    try:
        contents = {
            filename: (secret_directory / filename).read_text(encoding="ascii")
            for filename in _SECRET_FILENAMES
        }
    except OSError, UnicodeError:
        raise StackFailure(StackExitCode.CONTRACT, "invalid_secret_set") from None

    has_valid_passwords = all(
        _is_valid_secret_value(contents[spec.filename])
        for spec in SECRET_SPECS
        if spec.kind is SecretKind.PASSWORD
    )
    neo4j_auth = contents["neo4j_auth"]
    has_valid_neo4j_auth = neo4j_auth.startswith("neo4j/") and _is_valid_secret_value(
        neo4j_auth.removeprefix("neo4j/")
    )
    redis_acl = contents["redis_acl"]
    redis_prefix = "user default off\nuser knowledge on >"
    redis_suffix = " ~* +@all\n"
    redis_credential = redis_acl[len(redis_prefix) : -len(redis_suffix)]
    has_valid_redis_acl = (
        redis_acl.startswith(redis_prefix)
        and redis_acl.endswith(redis_suffix)
        and _is_valid_secret_value(redis_credential)
        and secrets.compare_digest(redis_credential, contents["redis_application_password"])
    )
    qdrant_key = contents["qdrant_api_key"]
    has_valid_qdrant_config = (
        contents[_QDRANT_CONFIG_FILENAME] == f"service:\n  api_key: {qdrant_key}\n"
    )
    if not (
        has_valid_passwords
        and has_valid_neo4j_auth
        and has_valid_redis_acl
        and has_valid_qdrant_config
    ):
        raise StackFailure(StackExitCode.CONTRACT, "invalid_secret_set")


def _is_valid_secret_value(value: str) -> bool:
    return (
        len(value) == _SECRET_BYTE_COUNT
        and all(character in _SECRET_ALPHABET for character in value)
        and len(set(value)) >= _MIN_DISTINCT_SECRET_CHARACTERS
    )


def _validate_private_mode(path_stat: os.stat_result, expected_mode: int) -> None:
    if sys.platform != "win32" and stat.S_IMODE(path_stat.st_mode) != expected_mode:
        raise StackFailure(StackExitCode.CONTRACT, "unsafe_secret_set")


def _create_or_validate_local_directory(local_directory: Path) -> None:
    try:
        local_directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_creation_failed") from None
    local_stat = _lstat_or_failure(local_directory, "secret_set_creation_failed")
    if local_stat is None:
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_creation_failed")
    _validate_private_directory(local_directory, local_stat, require_mode=True)


def _build_secret_contents(random_bytes: Callable[[int], bytes]) -> dict[str, bytes]:
    try:
        generated: dict[str, str] = {}
        for spec in SECRET_SPECS:
            if spec.kind is SecretKind.REDIS_ACL:
                continue
            password = _generate_secret_value(random_bytes)
            if spec.kind is SecretKind.PASSWORD:
                generated[spec.filename] = password
            else:
                generated[spec.filename] = f"neo4j/{password}"
        redis_password = generated["redis_application_password"]
        generated["redis_acl"] = f"user default off\nuser knowledge on >{redis_password} ~* +@all\n"
        qdrant_key = generated["qdrant_api_key"]
        generated[_QDRANT_CONFIG_FILENAME] = f"service:\n  api_key: {qdrant_key}\n"
        return {filename: content.encode("ascii") for filename, content in generated.items()}
    except Exception:
        raise StackFailure(StackExitCode.CONTRACT, "secret_generation_failed") from None


def _generate_secret_value(random_bytes: Callable[[int], bytes]) -> str:
    generated_bytes = random_bytes(_SECRET_BYTE_COUNT)
    if not isinstance(generated_bytes, bytes) or len(generated_bytes) != _SECRET_BYTE_COUNT:
        raise ValueError("invalid generated secret")
    return "".join(_SECRET_ALPHABET[byte & 0b0011_1111] for byte in generated_bytes)


def _write_private_secret_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    secret_file: BinaryIO | None = None
    try:
        secret_file = os.fdopen(descriptor, "wb")
        with secret_file:
            secret_file.write(content)
            secret_file.flush()
            os.fsync(secret_file.fileno())
    except Exception:
        if secret_file is None:
            with suppress(OSError):
                os.close(descriptor)
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_creation_failed") from None


def _flush_directory(directory: Path) -> None:
    if sys.platform == "win32":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_creation_failed") from None


def _set_private_mode(path: Path, mode: int) -> None:
    if sys.platform == "win32":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_creation_failed") from None


def _remove_staging_files(staging_directory: Path, created_files: Sequence[Path]) -> None:
    for created_file in reversed(created_files):
        with suppress(OSError):
            created_file.unlink()
    with suppress(OSError):
        staging_directory.rmdir()


def _parse_port(raw_port: str) -> int:
    if not raw_port.isascii() or not raw_port.isdecimal():
        raise StackFailure(StackExitCode.CLI, "invalid_port")
    port = int(raw_port)
    if not _MIN_PORT <= port <= _MAX_PORT:
        raise StackFailure(StackExitCode.CLI, "invalid_port")
    return port


def _read_bounded_output(stream: BinaryIO, buffer: bytearray) -> None:
    """Drain one pipe while retaining at most the safe diagnostics budget."""
    try:
        while chunk := stream.read(_MAX_CAPTURE_BYTES):
            remaining_bytes = _MAX_CAPTURE_BYTES - len(buffer)
            if remaining_bytes > 0:
                buffer.extend(chunk[:remaining_bytes])
    except OSError:
        return
    finally:
        with suppress(OSError):
            stream.close()


def _drain_readers_until_deadline(
    reader_threads: Sequence[threading.Thread], deadline_monotonic: float
) -> None:
    """Wait only until the command deadline for reader threads to finish draining.

    A descendant can retain a pipe after the direct child is terminated. Reader
    threads remain daemons so they can finish draining later without extending
    the lifecycle operation past its finite deadline.
    """
    for reader_thread in reader_threads:
        remaining_seconds = max(0.0, deadline_monotonic - time.monotonic())
        reader_thread.join(timeout=remaining_seconds)


class _HelpRequested(Exception):
    pass


class _CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise StackFailure(StackExitCode.CLI, "invalid_cli")

    def exit(self, status: int = 0, message: str | None = None) -> Never:
        if status == 0:
            if message:
                self._print_message(message, sys.stdout)
            raise _HelpRequested
        raise StackFailure(StackExitCode.CLI, "invalid_cli")


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = _CliParser(
        prog="local_service_stack",
        description="Operate the authenticated project-scoped local service stack.",
        exit_on_error=False,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("bootstrap", "config", "up", "status", "verify", "down"):
        command_parser = subparsers.add_parser(command, exit_on_error=False)
        command_parser.add_argument("--project-name", default="knowledge-local")
    smoke_parser = subparsers.add_parser("smoke", exit_on_error=False)
    smoke_parser.add_argument("--project-name", required=True)
    smoke_parser.add_argument("--confirm-project", required=True)
    reset_parser = subparsers.add_parser("reset", exit_on_error=False)
    reset_parser.add_argument("--project-name", required=True)
    reset_parser.add_argument("--confirm-project", required=True)
    reset_parser.add_argument("--rotate-secrets", action="store_true")
    reset_parser.add_argument("--non-interactive", action="store_true")
    return parser


def _cli_context(project_name: str) -> StackContext:
    environment = dict(os.environ)
    return StackContext(
        paths=resolve_stack_paths(Path(__file__).resolve().parents[1]),
        project_name=project_name,
        ports=resolve_ports(environment),
        environment=environment,
    )


def _execute_cli_command(
    arguments: argparse.Namespace,
    *,
    runner: CommandRunner,
    is_interactive: bool,
) -> tuple[dict[str, object], StackExitCode]:
    project_name = cast(str, arguments.project_name)
    context = _cli_context(project_name)
    command = cast(str, arguments.command)
    if command == "bootstrap":
        _validate_lifecycle_project(context)
        bootstrap_secret_set(
            context.paths,
            application_secret_references=context.application_secret_references,
        )
        return {
            "project": context.project_name,
            "state": "ready",
            "secret_set": "complete",
            "result_code": "secret_set_ready",
        }, StackExitCode.OK
    if command == "config":
        validate_compose_config(context, runner=runner)
        return {
            "project": context.project_name,
            "state": "valid",
            "result_code": "stack_config_valid",
        }, StackExitCode.OK
    if command == "up":
        status = stack_up(context, runner=runner)
        return status, StackExitCode.OK
    if command == "status":
        status = stack_status(context, runner=runner)
        exit_code = StackExitCode.CLI if status["state"] == "absent" else StackExitCode.OK
        return status, exit_code
    if command == "verify":
        probes = verify_stack(context, runner=runner)
        return {
            "project": context.project_name,
            "state": "ready",
            "probes": [asdict(probe) for probe in probes],
            "result_code": "stack_verified",
        }, StackExitCode.OK
    if command == "down":
        stack_down(context, runner=runner)
        return {
            "project": context.project_name,
            "state": "absent",
            "result_code": "stack_down_complete",
        }, StackExitCode.OK
    if command == "reset":
        is_noninteractive = cast(bool, arguments.non_interactive)
        if is_noninteractive:
            if context.environment.get("CI") != "true" or not context.project_name.startswith(
                "knowledge-ci-"
            ):
                raise StackFailure(StackExitCode.CLI, "noninteractive_reset_forbidden")
        elif not is_interactive:
            raise StackFailure(StackExitCode.CLI, "interactive_confirmation_required")
        return reset_stack(
            context,
            confirm_project=cast(str, arguments.confirm_project),
            rotate_secrets=cast(bool, arguments.rotate_secrets),
            runner=runner,
        ), StackExitCode.OK
    if command == "smoke":
        if cast(str, arguments.confirm_project) != context.project_name:
            raise StackFailure(StackExitCode.CLI, "smoke_confirmation_mismatch")
        run_smoke_contract(context, runner=runner)
        return {
            "project": context.project_name,
            "state": "absent",
            "result_code": "stack_smoke_complete",
        }, StackExitCode.OK
    raise StackFailure(StackExitCode.CLI, "invalid_cli")


def _print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CommandRunner = run_command,
    is_interactive: bool | None = None,
) -> int:
    """Run one lifecycle command and emit only one stable JSON result document."""
    parser = _build_cli_parser()
    try:
        parsed_arguments = parser.parse_args(argv)
        payload, exit_code = _execute_cli_command(
            parsed_arguments,
            runner=runner,
            is_interactive=sys.stdin.isatty() if is_interactive is None else is_interactive,
        )
    except _HelpRequested:
        return int(StackExitCode.OK)
    except (argparse.ArgumentError, StackFailure) as failure:
        if isinstance(failure, StackFailure):
            exit_code = failure.exit_code
            result_code = failure.result_code
            if exit_code is StackExitCode.CLI and result_code in {
                "duplicate_port",
                "invalid_port",
            }:
                exit_code = StackExitCode.PREREQUISITE
        else:
            exit_code = StackExitCode.CLI
            result_code = "invalid_cli"
        failure_payload = (
            dict(failure.diagnostic_payload) if isinstance(failure, StackFailure) else {}
        )
        _print_json({**failure_payload, "result_code": result_code, "state": "error"})
        return int(exit_code)
    except Exception:
        _print_json({"result_code": "lifecycle_internal_error", "state": "error"})
        return int(StackExitCode.INTERNAL)
    _print_json(payload)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
