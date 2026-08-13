"""Safe, typed local-service stack preconditions and subprocess boundary."""

from __future__ import annotations

import math
import os
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import BinaryIO, Protocol

_MAX_CAPTURE_BYTES = 8192
_MIN_PORT = 1024
_MAX_PORT = 65535
_PROJECT_NAME_PATTERN = re.compile(r"knowledge-(?:local|ci-[a-z0-9][a-z0-9-]{0,40})")


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
        process.kill()
        process.wait()
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
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        has_timed_out = True
        with suppress(OSError):
            process.kill()
        return_code = process.wait()
    finally:
        stdout_thread.join()
        stderr_thread.join()

    if has_timed_out:
        raise StackFailure(StackExitCode.READINESS, "subprocess_timeout")

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
