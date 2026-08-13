"""Safe, typed local-service stack preconditions and subprocess boundary."""

from __future__ import annotations

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
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import BinaryIO, Final, Protocol

_MAX_CAPTURE_BYTES = 8192
_MIN_PORT = 1024
_MAX_PORT = 65535
_PROJECT_NAME_PATTERN = re.compile(r"knowledge-(?:local|ci-[a-z0-9][a-z0-9-]{0,40})")
_SECRET_ALPHABET: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_SECRET_BYTE_COUNT: Final = 32
_MIN_DISTINCT_SECRET_CHARACTERS: Final = 8
_QDRANT_CONFIG_FILENAME: Final = "qdrant_config.yaml"


class StackExitCode(IntEnum):
    """Stable exit codes for local-stack lifecycle operations."""

    OK = 0
    CLI = 2
    PREREQUISITE = 64
    CONTRACT = 65
    STARTUP = 69
    INTERNAL = 70
    READINESS = 75


@dataclass(frozen=True, slots=True)
class StackFailure(Exception):
    """A safe lifecycle failure that carries no raw dependency detail."""

    exit_code: StackExitCode
    result_code: str

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


class CommandRunner(Protocol):
    """Callable contract for bounded lifecycle command execution."""

    def __call__(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


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
        "TMP",
        "TEMP",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "CI",
        *_PORT_VARIABLES,
    }
)


def inspect_secret_set(paths: StackPaths) -> SecretSetState:
    """Return the safe state of the exact local secret set without reading values."""
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

    found_filenames: set[str] = set()
    child_stats: list[tuple[Path, os.stat_result]] = []
    try:
        children = tuple(secret_directory.iterdir())
    except OSError:
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_inspection_failed") from None
    for child in children:
        child_stat = _lstat_or_failure(child, "secret_set_inspection_failed")
        if child_stat is None:
            raise StackFailure(StackExitCode.CONTRACT, "secret_set_inspection_failed")
        _validate_private_file(child, child_stat)
        child_stats.append((child, child_stat))
        found_filenames.add(child.name)

    if found_filenames == _SECRET_FILENAMES:
        _validate_private_directory(local_directory, local_stat, require_mode=True)
        _validate_private_directory(secret_directory, secret_stat, require_mode=True)
        for child, child_stat in child_stats:
            _validate_private_file(child, child_stat, require_mode=True)
        _validate_complete_secret_contents(secret_directory)
        return SecretSetState.COMPLETE
    return SecretSetState.PARTIAL


def bootstrap_secret_set(
    paths: StackPaths, *, random_bytes: Callable[[int], bytes] = secrets.token_bytes
) -> SecretSetState:
    """Atomically create or safely reuse the complete local secret set."""
    state = inspect_secret_set(paths)
    if state is SecretSetState.COMPLETE:
        return state
    if state is SecretSetState.PARTIAL:
        raise StackFailure(StackExitCode.CONTRACT, "partial_secret_set")

    secret_directory = _validate_secret_directory_location(paths)
    local_directory = secret_directory.parent
    _create_or_validate_local_directory(local_directory)
    staging_directory: Path | None = None
    created_files: list[Path] = []
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
        os.rename(staging_directory, secret_directory)
        staging_directory = None
        has_renamed_secret_set = True
        _flush_directory(local_directory)
    except StackFailure:
        if has_renamed_secret_set:
            try:
                if inspect_secret_set(paths) is SecretSetState.COMPLETE:
                    return SecretSetState.COMPLETE
            except StackFailure:
                pass
        raise
    except (OSError, ValueError):
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_creation_failed") from None
    finally:
        if staging_directory is not None:
            _remove_staging_files(staging_directory, created_files)

    state = inspect_secret_set(paths)
    if state is not SecretSetState.COMPLETE:
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_creation_failed")
    return state


def validate_secret_set(
    paths: StackPaths, *, list_project_volumes: Callable[[], Sequence[str]]
) -> SecretSetState:
    """Refuse missing credentials when existing volumes could depend on them."""
    state = inspect_secret_set(paths)
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


def remove_secret_set_after_reset(paths: StackPaths) -> SecretSetState:
    """Remove only an exact complete set after its project reset has succeeded."""
    state = inspect_secret_set(paths)
    if state is SecretSetState.MISSING:
        return state
    if state is SecretSetState.PARTIAL:
        raise StackFailure(StackExitCode.CONTRACT, "partial_secret_set")

    secret_directory = _validate_secret_directory_location(paths)
    try:
        for filename in _SECRET_FILENAMES:
            (secret_directory / filename).unlink()
        secret_directory.rmdir()
    except OSError:
        raise StackFailure(StackExitCode.CONTRACT, "secret_set_removal_failed") from None
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
    with suppress(OSError, ValueError):
        process = subprocess.Popen(
            list(arguments),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=clean_environment,
        )
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
    except (OSError, ValueError):
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
    """Compare the path owner SID with the current process-token user SID."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        token_query = 0x0008
        token_user = 1
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

        class SidAndAttributes(ctypes.Structure):
            _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

        class TokenUser(ctypes.Structure):
            _fields_ = [("user", SidAndAttributes)]

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
        ):
            return False
        try:
            required_size = wintypes.DWORD()
            advapi32.GetTokenInformation(
                token, token_user, None, 0, ctypes.byref(required_size)
            )
            if ctypes.get_last_error() != error_insufficient_buffer or not required_size.value:
                return False
            token_buffer = ctypes.create_string_buffer(required_size.value)
            if not advapi32.GetTokenInformation(
                token,
                token_user,
                ctypes.cast(token_buffer, ctypes.c_void_p),
                required_size,
                ctypes.byref(required_size),
            ):
                return False
            token_user_data = ctypes.cast(
                token_buffer, ctypes.POINTER(TokenUser)
            ).contents
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
                return bool(advapi32.EqualSid(owner_sid, token_user_data.user.sid))
            finally:
                if security_descriptor.value:
                    kernel32.LocalFree(security_descriptor)
        finally:
            kernel32.CloseHandle(token)
    except (AttributeError, OSError, TypeError):
        return False


def _validate_complete_secret_contents(secret_directory: Path) -> None:
    try:
        contents = {
            filename: (secret_directory / filename).read_text(encoding="ascii")
            for filename in _SECRET_FILENAMES
        }
    except (OSError, UnicodeError):
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
    has_valid_redis_acl = (
        redis_acl.startswith(redis_prefix)
        and redis_acl.endswith(redis_suffix)
        and _is_valid_secret_value(redis_acl[len(redis_prefix) : -len(redis_suffix)])
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
            password = _generate_secret_value(random_bytes)
            if spec.kind is SecretKind.PASSWORD:
                generated[spec.filename] = password
            elif spec.kind is SecretKind.NEO4J_AUTH:
                generated[spec.filename] = f"neo4j/{password}"
            else:
                generated[spec.filename] = (
                    f"user default off\nuser knowledge on >{password} ~* +@all\n"
                )
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
