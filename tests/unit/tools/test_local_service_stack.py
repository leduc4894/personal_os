from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from tools.local_service_stack import (
    StackExitCode,
    StackFailure,
    resolve_ports,
    resolve_stack_paths,
    run_command,
    sanitize_subprocess_environment,
    validate_port_availability,
    validate_project_name,
)


def test_resolve_stack_paths_stays_beneath_repository(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)

    assert paths.compose_file == tmp_path / "infra" / "compose" / "compose.yaml"
    assert paths.image_lock == tmp_path / "infra" / "compose" / "images.lock.yaml"
    assert paths.secret_directory == tmp_path / ".local" / "stack-secrets"
    assert paths.state_directory == tmp_path / ".local" / "stack-state"


@pytest.mark.parametrize("name", ["knowledge-local", "knowledge-ci-a1b2c3"])
def test_accepts_bounded_project_name(name: str) -> None:
    assert validate_project_name(name) == name


@pytest.mark.parametrize("name", ["", "Knowledge", "../escape", "a" * 64])
def test_rejects_unsafe_project_name(name: str) -> None:
    with pytest.raises(StackFailure) as raised:
        validate_project_name(name)

    assert raised.value.exit_code is StackExitCode.CLI
    assert str(raised.value) == "invalid_project_name"


def test_resolves_all_documented_default_ports() -> None:
    ports = resolve_ports({})

    assert ports == {
        "POSTGRES_PORT": 5432,
        "QDRANT_HTTP_PORT": 6333,
        "QDRANT_GRPC_PORT": 6334,
        "NEO4J_HTTP_PORT": 7474,
        "NEO4J_BOLT_PORT": 7687,
        "REDIS_PORT": 6379,
        "TEMPORAL_GRPC_PORT": 7233,
        "TEMPORAL_UI_PORT": 8080,
    }


@pytest.mark.parametrize(
    "override", ["1023", "65536", " 5432", "+5432", "5_432", "\uff11\uff12\uff13\uff14"]
)
def test_rejects_invalid_port_override(override: str) -> None:
    with pytest.raises(StackFailure) as raised:
        resolve_ports({"POSTGRES_PORT": override})

    assert raised.value.exit_code is StackExitCode.CLI
    assert str(raised.value) == "invalid_port"


def test_rejects_duplicate_effective_ports() -> None:
    with pytest.raises(StackFailure, match="duplicate_port"):
        resolve_ports({"POSTGRES_PORT": "15432", "REDIS_PORT": "15432"})


class _TrackedSocket:
    def __init__(self, *, bind_fails: bool) -> None:
        self.bind_fails = bind_fails
        self.closed = False
        self.options: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def bind(self, address: tuple[str, int]) -> None:
        if self.bind_fails:
            raise OSError("already in use")

    def close(self) -> None:
        self.closed = True


def test_closes_every_opened_socket_when_port_binding_fails() -> None:
    pending = [_TrackedSocket(bind_fails=False), _TrackedSocket(bind_fails=True)]
    created: list[_TrackedSocket] = []

    def socket_factory(family: int, kind: int) -> _TrackedSocket:
        assert family == socket.AF_INET
        assert kind == socket.SOCK_STREAM
        opened_socket = pending.pop(0)
        created.append(opened_socket)
        return opened_socket

    with pytest.raises(StackFailure) as raised:
        validate_port_availability(
            {"POSTGRES_PORT": 15432, "REDIS_PORT": 16379}, socket_factory=socket_factory
        )

    assert raised.value.exit_code is StackExitCode.PREREQUISITE
    assert str(raised.value) == "port_unavailable"
    assert all(opened_socket.closed for opened_socket in created)


def test_subprocess_environment_omits_credentials_and_r2() -> None:
    clean = sanitize_subprocess_environment(
        {"PATH": "safe", "R2_SECRET_ACCESS_KEY": "secret", "POSTGRES_PASSWORD": "secret"}
    )

    assert clean == {"PATH": "safe"}


def test_run_command_maps_timeout_without_raw_exception() -> None:
    with pytest.raises(StackFailure) as raised:
        run_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout_seconds=0.01)

    assert raised.value.exit_code is StackExitCode.READINESS
    assert str(raised.value) == "subprocess_timeout"


def test_run_command_maps_missing_program_without_raw_exception() -> None:
    with pytest.raises(StackFailure) as raised:
        run_command(["missing-local-stack-prerequisite"], timeout_seconds=1.0)

    assert raised.value.exit_code is StackExitCode.PREREQUISITE
    assert str(raised.value) == "subprocess_unavailable"


def test_run_command_truncates_captured_output() -> None:
    result = run_command(
        [sys.executable, "-c", "import sys; print('x' * 9000); print('y' * 9000, file=sys.stderr)"],
        timeout_seconds=1.0,
    )

    assert result.return_code == 0
    assert len(result.stdout.encode("utf-8")) == 8192
    assert len(result.stderr.encode("utf-8")) == 8192
