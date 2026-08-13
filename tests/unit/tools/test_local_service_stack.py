from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from tools import local_service_stack as stack_module
from tools.local_service_stack import (
    SECRET_SPECS,
    SecretSetState,
    StackExitCode,
    StackFailure,
    bootstrap_secret_set,
    inspect_secret_set,
    remove_secret_set_after_reset,
    resolve_ports,
    resolve_stack_paths,
    run_command,
    sanitize_subprocess_environment,
    validate_port_availability,
    validate_project_name,
    validate_secret_set,
)


def test_bootstrap_creates_exact_complete_secret_set(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)

    assert (
        bootstrap_secret_set(paths, random_bytes=lambda count: bytes(range(count)))
        is SecretSetState.COMPLETE
    )
    assert {path.name for path in paths.secret_directory.iterdir()} == {
        spec.filename for spec in SECRET_SPECS
    } | {"qdrant_config.yaml"}


def test_bootstrap_reuses_complete_set_byte_for_byte(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    bootstrap_secret_set(paths)
    before = {path.name: path.read_bytes() for path in paths.secret_directory.iterdir()}

    assert bootstrap_secret_set(paths) is SecretSetState.COMPLETE
    assert {path.name: path.read_bytes() for path in paths.secret_directory.iterdir()} == before


def test_partial_secret_set_is_terminal_and_not_rewritten(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    paths.secret_directory.mkdir(parents=True)
    partial = paths.secret_directory / "postgres_admin_password"
    partial.write_text("unchanged", encoding="ascii")

    with pytest.raises(StackFailure, match="partial_secret_set"):
        bootstrap_secret_set(paths)

    assert partial.read_text(encoding="ascii") == "unchanged"


def test_missing_secrets_with_existing_volume_is_terminal(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)

    with pytest.raises(StackFailure, match="secret_set_missing_with_volumes"):
        validate_secret_set(paths, list_project_volumes=lambda: ("knowledge-local_postgres-data",))


def test_secret_values_use_required_native_shapes_and_boundaries(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    bootstrap_secret_set(paths, random_bytes=lambda count: bytes(range(count)))
    values = {
        path.name: path.read_text(encoding="ascii") for path in paths.secret_directory.iterdir()
    }
    password_names = {
        "postgres_admin_password",
        "postgres_application_password",
        "postgres_temporal_password",
        "qdrant_api_key",
        "redis_application_password",
    }

    alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert all(set(values[name]) <= alphabet for name in password_names)
    assert all(len(values[name]) == 32 and "\n" not in values[name] for name in password_names)
    assert values["neo4j_auth"].startswith("neo4j/")
    assert len(values["neo4j_auth"]) == len("neo4j/") + 32
    assert "\n" not in values["neo4j_auth"]
    assert values["redis_acl"].startswith("user default off\nuser knowledge on >")
    assert values["redis_acl"].endswith(" ~* +@all\n")
    assert values["redis_acl"].count("\n") == 2
    assert values["qdrant_config.yaml"].startswith("service:\n  api_key: ")
    assert values["qdrant_config.yaml"].endswith("\n")
    assert not values["qdrant_config.yaml"].endswith("\n\n")
    assert all("r2" not in name.lower() for name in values)


def test_secret_set_rejects_symlink_without_leaking_secret_or_path(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    paths.secret_directory.mkdir(parents=True)
    secret_value = "secret-value-that-must-not-leak"
    target = tmp_path / "target"
    target.write_text(secret_value, encoding="ascii")
    link = paths.secret_directory / "postgres_admin_password"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(StackFailure) as raised:
        bootstrap_secret_set(paths)

    assert str(raised.value) == "unsafe_secret_set"
    assert secret_value not in str(raised.value)
    assert str(link) not in str(raised.value)
    assert link.name not in str(raised.value)


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX permission bits are not a Windows contract"
)
def test_created_secret_set_uses_private_posix_permissions(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    bootstrap_secret_set(paths)

    assert paths.secret_directory.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths.secret_directory.iterdir())


def test_windows_boundary_accepts_regular_non_symlink_secret_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = resolve_stack_paths(tmp_path)
    bootstrap_secret_set(paths)
    monkeypatch.setattr(sys, "platform", "win32")

    assert inspect_secret_set(paths) is SecretSetState.COMPLETE


def test_remove_secret_set_after_reset_removes_only_complete_set(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    bootstrap_secret_set(paths)

    assert remove_secret_set_after_reset(paths) is SecretSetState.MISSING
    assert not paths.secret_directory.exists()


@pytest.mark.parametrize(
    ("filename", "replacement"),
    [
        ("postgres_admin_password", ""),
        ("postgres_application_password", "A" * 32),
        ("neo4j_auth", "not-a-native-neo4j-auth"),
        ("qdrant_config.yaml", "service:\n  api_key: mismatched-key\n"),
    ],
)
def test_existing_secret_content_must_be_complete_native_and_nontrivial(
    tmp_path: Path, filename: str, replacement: str
) -> None:
    paths = resolve_stack_paths(tmp_path)
    bootstrap_secret_set(paths)
    (paths.secret_directory / filename).write_text(replacement, encoding="ascii")

    with pytest.raises(StackFailure) as raised:
        bootstrap_secret_set(paths)

    assert str(raised.value) == "invalid_secret_set"
    assert filename not in str(raised.value)
    if replacement:
        assert replacement not in str(raised.value)


def test_rejects_secret_directory_reparse_redirect_before_inspection(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    redirect = tmp_path / "redirect"
    redirect.mkdir()
    local_directory = paths.secret_directory.parent
    try:
        local_directory.symlink_to(redirect, target_is_directory=True)
    except OSError:
        pytest.skip("directory reparse-point creation is unavailable")

    with pytest.raises(StackFailure) as raised:
        inspect_secret_set(paths)

    assert str(raised.value) == "invalid_secret_directory"
    assert str(local_directory) not in str(raised.value)


def test_windows_owner_boundary_refuses_non_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = resolve_stack_paths(tmp_path)
    bootstrap_secret_set(paths)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(stack_module, "_is_current_windows_user_owner", lambda path: False)

    with pytest.raises(StackFailure, match="unsafe_secret_set"):
        inspect_secret_set(paths)


def test_parent_flush_failure_after_rename_returns_reusable_complete_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = resolve_stack_paths(tmp_path)
    flush_directory = stack_module._flush_directory

    def fail_only_parent(directory: Path) -> None:
        if directory == paths.secret_directory.parent and paths.secret_directory.exists():
            raise StackFailure(StackExitCode.CONTRACT, "secret_set_creation_failed")
        flush_directory(directory)

    monkeypatch.setattr(stack_module, "_flush_directory", fail_only_parent)

    assert bootstrap_secret_set(paths) is SecretSetState.COMPLETE
    assert inspect_secret_set(paths) is SecretSetState.COMPLETE


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
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
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


class _ReaderHeldPipe:
    def __init__(self, release_reader: threading.Event) -> None:
        self.release_reader = release_reader

    def read(self, size: int) -> bytes:
        self.release_reader.wait()
        return b""

    def close(self) -> None:
        return None


class _TimedOutProcess:
    def __init__(self, release_reader: threading.Event) -> None:
        self.stdout = _ReaderHeldPipe(release_reader)
        self.stderr = _ReaderHeldPipe(release_reader)
        self.was_killed = False

    def wait(self, timeout: float | None = None) -> int:
        if not self.was_killed:
            raise subprocess.TimeoutExpired(["blocked-reader"], timeout)
        return -9

    def kill(self) -> None:
        self.was_killed = True


def test_timeout_does_not_wait_for_readers_held_after_process_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_reader = threading.Event()
    process = _TimedOutProcess(release_reader)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    release_timer = threading.Timer(0.2, release_reader.set)
    release_timer.start()

    try:
        started_at = time.monotonic()
        with pytest.raises(StackFailure) as raised:
            run_command(["blocked-reader"], timeout_seconds=0.01)
        elapsed_seconds = time.monotonic() - started_at
    finally:
        release_reader.set()
        release_timer.cancel()

    assert raised.value.exit_code is StackExitCode.READINESS
    assert str(raised.value) == "subprocess_timeout"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert process.was_killed
    assert elapsed_seconds < 0.1


def test_run_command_maps_missing_program_without_raw_exception() -> None:
    with pytest.raises(StackFailure) as raised:
        run_command(["missing-local-stack-prerequisite"], timeout_seconds=1.0)

    assert raised.value.exit_code is StackExitCode.PREREQUISITE
    assert str(raised.value) == "subprocess_unavailable"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_run_command_rejects_invalid_port_before_process_launch() -> None:
    with pytest.raises(StackFailure) as raised:
        run_command(
            [sys.executable, "-c", "raise SystemExit(0)"],
            timeout_seconds=1.0,
            environment={"POSTGRES_PORT": "1023"},
        )

    assert raised.value.exit_code is StackExitCode.CLI
    assert str(raised.value) == "invalid_port"


def test_run_command_captures_output_without_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_unbounded_capture(*args: object, **kwargs: object) -> None:
        pytest.fail("subprocess.run buffers unbounded output")

    monkeypatch.setattr(subprocess, "run", fail_unbounded_capture)

    result = run_command(
        [sys.executable, "-c", "import sys; print('x' * 9000); print('y' * 9000, file=sys.stderr)"],
        timeout_seconds=1.0,
    )

    assert result.return_code == 0
    assert len(result.stdout.encode("utf-8")) == 8192
    assert len(result.stderr.encode("utf-8")) == 8192


def test_run_command_truncates_captured_output() -> None:
    result = run_command(
        [sys.executable, "-c", "import sys; print('x' * 9000); print('y' * 9000, file=sys.stderr)"],
        timeout_seconds=1.0,
    )

    assert result.return_code == 0
    assert len(result.stdout.encode("utf-8")) == 8192
    assert len(result.stderr.encode("utf-8")) == 8192
