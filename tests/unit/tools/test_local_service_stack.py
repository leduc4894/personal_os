from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
import yaml

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


def _valid_lock_document() -> dict[str, Any]:
    return {
        "version": 1,
        "images": [
            {
                "component": "postgresql",
                "upstream_repository": "postgres",
                "version": "18.4-bookworm",
                "tagged_reference": "postgres:18.4-bookworm",
                "manifest_digest": f"sha256:{'a' * 64}",
                "supported_platforms": ["linux/amd64"],
                "verified_at": "2026-08-13",
            }
        ],
    }


def _write_yaml(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_load_image_lock_returns_typed_immutable_entries(tmp_path: Path) -> None:
    lock_path = tmp_path / "images.lock.yaml"
    _write_yaml(lock_path, _valid_lock_document())

    entries = stack_module.load_image_lock(lock_path)

    assert len(entries) == 1
    assert entries[0].component == "postgresql"
    assert entries[0].locked_reference == f"postgres:18.4-bookworm@sha256:{'a' * 64}"
    assert entries[0].supported_platforms == ("linux/amd64",)


def test_image_lock_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    lock_path = tmp_path / "images.lock.yaml"
    document = _valid_lock_document()
    document["unexpected"] = True
    _write_yaml(lock_path, document)

    with pytest.raises(StackFailure) as raised:
        stack_module.load_image_lock(lock_path)

    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "image_lock_invalid"


def test_image_lock_rejects_unknown_entry_key(tmp_path: Path) -> None:
    lock_path = tmp_path / "images.lock.yaml"
    document = _valid_lock_document()
    document["images"][0]["unexpected"] = True
    _write_yaml(lock_path, document)

    with pytest.raises(StackFailure, match="image_lock_invalid"):
        stack_module.load_image_lock(lock_path)


def test_image_lock_rejects_duplicate_components(tmp_path: Path) -> None:
    lock_path = tmp_path / "images.lock.yaml"
    document = _valid_lock_document()
    duplicate = dict(document["images"][0])
    duplicate["tagged_reference"] = "example.invalid/postgres:18.4-bookworm"
    duplicate["upstream_repository"] = "example.invalid/postgres"
    document["images"].append(duplicate)
    _write_yaml(lock_path, document)

    with pytest.raises(StackFailure, match="image_lock_invalid"):
        stack_module.load_image_lock(lock_path)


def test_image_lock_rejects_duplicate_tagged_references(tmp_path: Path) -> None:
    lock_path = tmp_path / "images.lock.yaml"
    document = _valid_lock_document()
    duplicate = dict(document["images"][0])
    duplicate["component"] = "postgresql-copy"
    document["images"].append(duplicate)
    _write_yaml(lock_path, document)

    with pytest.raises(StackFailure, match="image_lock_invalid"):
        stack_module.load_image_lock(lock_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manifest_digest", "sha256:not-a-digest"),
        ("supported_platforms", ["linux/arm64"]),
        ("supported_platforms", ["linux/amd64", "linux/arm64"]),
    ],
)
def test_image_lock_rejects_invalid_digest_or_platform(
    tmp_path: Path, field: str, value: object
) -> None:
    lock_path = tmp_path / "images.lock.yaml"
    document = _valid_lock_document()
    document["images"][0][field] = value
    _write_yaml(lock_path, document)

    with pytest.raises(StackFailure, match="image_lock_invalid"):
        stack_module.load_image_lock(lock_path)


def test_validate_image_lock_rejects_wrong_digest_without_echoing_reference(
    tmp_path: Path,
) -> None:
    paths = resolve_stack_paths(tmp_path)
    bad_reference = f"postgres:18.4-bookworm@sha256:{'b' * 64}"
    document = _valid_lock_document()
    document["images"][0]["manifest_digest"] = f"sha256:{'b' * 64}"
    _write_yaml(paths.image_lock, document)
    _write_yaml(
        paths.compose_file,
        {
            "services": {
                "postgresql": {
                    "image": f"postgres:18.4-bookworm@sha256:{'a' * 64}",
                    "platform": "linux/amd64",
                }
            }
        },
    )

    with pytest.raises(StackFailure) as raised:
        stack_module.validate_image_lock(paths)

    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "image_lock_mismatch"
    assert bad_reference not in str(raised.value)


def test_validate_image_lock_rejects_unconsumed_entry(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    document = _valid_lock_document()
    extra_entry = dict(document["images"][0])
    extra_entry.update(
        {
            "component": "redis",
            "upstream_repository": "redis",
            "version": "8.6.4",
            "tagged_reference": "redis:8.6.4",
            "manifest_digest": f"sha256:{'c' * 64}",
        }
    )
    document["images"].append(extra_entry)
    _write_yaml(paths.image_lock, document)
    _write_yaml(
        paths.compose_file,
        {
            "services": {
                "postgresql": {
                    "image": f"postgres:18.4-bookworm@sha256:{'a' * 64}",
                    "platform": "linux/amd64",
                }
            }
        },
    )

    with pytest.raises(StackFailure, match="image_lock_mismatch"):
        stack_module.validate_image_lock(paths)


def test_validate_image_lock_accepts_one_entry_reused_by_multiple_init_jobs(
    tmp_path: Path,
) -> None:
    paths = resolve_stack_paths(tmp_path)
    document = _valid_lock_document()
    document["images"][0].update(
        {
            "component": "temporal-admin-tools",
            "upstream_repository": "temporalio/admin-tools",
            "version": "1.31.2",
            "tagged_reference": "temporalio/admin-tools:1.31.2",
        }
    )
    locked_reference = f"temporalio/admin-tools:1.31.2@sha256:{'a' * 64}"
    _write_yaml(paths.image_lock, document)
    _write_yaml(
        paths.compose_file,
        {
            "services": {
                "temporal-schema-setup": {
                    "image": locked_reference,
                    "platform": "linux/amd64",
                },
                "temporal-namespace-bootstrap": {
                    "image": locked_reference,
                    "platform": "linux/amd64",
                },
            }
        },
    )

    entries = stack_module.validate_image_lock(paths)

    assert len(entries) == 1
    assert entries[0].component == "temporal-admin-tools"


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


def test_subprocess_environment_retains_windows_compose_plugin_root() -> None:
    clean = sanitize_subprocess_environment(
        {"PROGRAMFILES": r"C:\Program Files", "R2_SECRET_ACCESS_KEY": "secret"}
    )

    assert clean == {"PROGRAMFILES": r"C:\Program Files"}


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


_RUNTIME_SERVICES = (
    "neo4j",
    "postgresql",
    "qdrant",
    "redis",
    "temporal",
    "temporal-cli",
    "temporal-ui",
)
_INITIALIZER_SERVICES = (
    "postgres-provision",
    "temporal-namespace-bootstrap",
    "temporal-schema-setup",
)


@pytest.fixture
def stack_context(tmp_path: Path) -> Any:
    paths = resolve_stack_paths(tmp_path)
    _write_yaml(paths.image_lock, _valid_lock_document())
    _write_yaml(
        paths.compose_file,
        {
            "services": {
                "postgresql": {
                    "image": f"postgres:18.4-bookworm@sha256:{'a' * 64}",
                    "platform": "linux/amd64",
                }
            }
        },
    )
    bootstrap_secret_set(paths, random_bytes=lambda count: bytes(range(count)))
    ports = {
        binding.variable: 15000 + index for index, binding in enumerate(stack_module.PORT_BINDINGS)
    }
    return stack_module.StackContext(
        paths=paths,
        project_name="knowledge-local",
        ports=ports,
        environment={"PATH": "safe", "R2_SECRET_ACCESS_KEY": "must-not-be-forwarded"},
    )


def _healthy_ps_output() -> str:
    rows = [
        {"Service": service, "State": "running", "Health": "healthy", "ExitCode": 0}
        for service in _RUNTIME_SERVICES
    ]
    rows.extend(
        {"Service": service, "State": "exited", "Health": "", "ExitCode": 0}
        for service in _INITIALIZER_SERVICES
    )
    return json.dumps(rows)


def _starting_ps_output() -> str:
    rows = [
        {"Service": service, "State": "running", "Health": "starting", "ExitCode": 0}
        for service in _RUNTIME_SERVICES
    ]
    rows.extend(
        {"Service": service, "State": "running", "Health": "", "ExitCode": 0}
        for service in _INITIALIZER_SERVICES
    )
    return json.dumps(rows)


def _successful_lifecycle_runner(
    calls: list[tuple[tuple[str, ...], float, dict[str, str]]] | None = None,
    *,
    ps_output: str | None = None,
) -> Any:
    def run(
        arguments: Any,
        *,
        timeout_seconds: float,
        environment: Any = None,
    ) -> Any:
        command = tuple(arguments)
        forwarded_environment = dict(environment or {})
        if calls is not None:
            calls.append((command, timeout_seconds, forwarded_environment))
        if command[:2] == ("docker", "compose") and command[-2:] == ("version", "--short"):
            return stack_module.CommandResult(0, "2.30.0\n", "")
        if command[:3] == ("docker", "version", "--format"):
            return stack_module.CommandResult(0, "linux/amd64\n", "")
        if "ps" in command:
            return stack_module.CommandResult(0, ps_output or _healthy_ps_output(), "")
        return stack_module.CommandResult(0, "", "")

    return run


def test_stack_context_is_frozen_and_copies_mutable_inputs(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    ports = {"POSTGRES_PORT": 15432}
    environment = {"PATH": "safe"}

    context = stack_module.StackContext(paths, "knowledge-local", ports, environment)
    ports["POSTGRES_PORT"] = 25432
    environment["PATH"] = "changed"

    assert context.ports == {"POSTGRES_PORT": 15432}
    assert context.environment == {"PATH": "safe"}
    with pytest.raises(FrozenInstanceError):
        context.project_name = "knowledge-ci-mutated"
    with pytest.raises(TypeError):
        context.ports["POSTGRES_PORT"] = 35432


def test_compose_arguments_are_array_based_and_project_scoped(stack_context: Any) -> None:
    assert stack_module.compose_arguments(stack_context) == [
        "docker",
        "compose",
        "--file",
        str(stack_context.paths.compose_file),
        "--project-name",
        "knowledge-local",
    ]


def test_every_compose_invocation_is_explicitly_project_scoped(
    monkeypatch: pytest.MonkeyPatch, stack_context: Any
) -> None:
    calls: list[tuple[tuple[str, ...], float, dict[str, str]]] = []
    monkeypatch.setattr(stack_module, "validate_port_availability", lambda ports: None)

    stack_module.stack_up(stack_context, runner=_successful_lifecycle_runner(calls))

    compose_prefix = tuple(stack_module.compose_arguments(stack_context))
    compose_calls = [command for command, _, _ in calls if command[:2] == ("docker", "compose")]
    assert compose_calls
    assert all(command[: len(compose_prefix)] == compose_prefix for command in compose_calls)


@pytest.mark.parametrize("version", ["2.29.0", "v2.29.99", "Docker Compose version v2.29.7"])
def test_prerequisites_reject_compose_before_minimum(stack_context: Any, version: str) -> None:
    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        return stack_module.CommandResult(0, version, "")

    with pytest.raises(StackFailure) as raised:
        stack_module.check_prerequisites(stack_context, runner=runner, require_engine=False)

    assert raised.value.exit_code is StackExitCode.PREREQUISITE
    assert str(raised.value) == "compose_version_unsupported"


def test_prerequisites_accept_compose_2_30_0(stack_context: Any) -> None:
    versions = stack_module.check_prerequisites(
        stack_context,
        runner=_successful_lifecycle_runner(),
        require_engine=False,
    )

    assert versions == stack_module.PrerequisiteVersions((2, 30, 0), "", "")


@pytest.mark.parametrize(
    ("engine", "result_code"),
    [
        ("windows/amd64", "engine_os_unsupported"),
        ("linux/arm64", "engine_architecture_unsupported"),
    ],
)
def test_prerequisites_reject_unsupported_engine(
    stack_context: Any, engine: str, result_code: str
) -> None:
    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        if tuple(arguments)[-2:] == ("version", "--short"):
            return stack_module.CommandResult(0, "2.30.0", "")
        return stack_module.CommandResult(0, engine, "")

    with pytest.raises(StackFailure) as raised:
        stack_module.check_prerequisites(stack_context, runner=runner)

    assert raised.value.exit_code is StackExitCode.PREREQUISITE
    assert str(raised.value) == result_code


def test_daemon_unavailable_maps_to_prerequisite(stack_context: Any) -> None:
    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        if tuple(arguments)[-2:] == ("version", "--short"):
            return stack_module.CommandResult(0, "2.30.0", "")
        return stack_module.CommandResult(1, "daemon output must not escape", "secret error")

    with pytest.raises(StackFailure) as raised:
        stack_module.check_prerequisites(stack_context, runner=runner)

    assert raised.value.exit_code is StackExitCode.PREREQUISITE
    assert str(raised.value) == "engine_unavailable"
    assert "daemon" not in str(raised.value)
    assert "secret" not in str(raised.value)


def test_ci_project_is_rejected_without_ci_environment(stack_context: Any) -> None:
    context = stack_module.StackContext(
        stack_context.paths,
        "knowledge-ci-run-1",
        stack_context.ports,
        {"PATH": "safe"},
    )
    calls: list[tuple[tuple[str, ...], float, dict[str, str]]] = []

    with pytest.raises(StackFailure) as raised:
        stack_module.stack_status(context, runner=_successful_lifecycle_runner(calls))

    assert raised.value.exit_code is StackExitCode.CLI
    assert str(raised.value) == "ci_project_requires_ci"
    assert calls == []


def test_config_validation_does_not_contact_engine(
    monkeypatch: pytest.MonkeyPatch, stack_context: Any
) -> None:
    calls: list[tuple[tuple[str, ...], float, dict[str, str]]] = []
    monkeypatch.setattr(stack_module, "validate_port_availability", lambda ports: None)

    stack_module.validate_compose_config(stack_context, runner=_successful_lifecycle_runner(calls))

    commands = [call[0] for call in calls]
    assert any(command[-2:] == ("version", "--short") for command in commands)
    assert not any(command[:3] == ("docker", "version", "--format") for command in commands)
    assert commands[-1] == (
        *stack_module.compose_arguments(stack_context),
        "config",
        "--quiet",
    )


def test_up_preflight_order_precedes_first_mutating_command(
    monkeypatch: pytest.MonkeyPatch, stack_context: Any
) -> None:
    operations: list[str] = []
    original_validate_project = stack_module.validate_project_name

    def validate_project(name: str) -> str:
        operations.append("validate_project")
        return original_validate_project(name)

    def validate_ports(ports: Any) -> None:
        operations.append("validate_ports")

    def validate_secrets(paths: Any, *, list_project_volumes: Any) -> Any:
        operations.append("validate_secrets")
        return SecretSetState.COMPLETE

    def validate_lock(paths: Any) -> tuple[Any, ...]:
        operations.append("validate_lock")
        return ()

    monkeypatch.setattr(stack_module, "validate_project_name", validate_project)
    monkeypatch.setattr(stack_module, "validate_port_availability", validate_ports)
    monkeypatch.setattr(stack_module, "validate_secret_set", validate_secrets)
    monkeypatch.setattr(stack_module, "validate_image_lock", validate_lock)

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        command = tuple(arguments)
        if command[:2] == ("docker", "compose") and command[-2:] == ("version", "--short"):
            operations.append("check_compose")
            return stack_module.CommandResult(0, "2.30.0", "")
        if command[:3] == ("docker", "version", "--format"):
            operations.append("check_engine")
            return stack_module.CommandResult(0, "linux/amd64", "")
        if "config" in command:
            operations.append("compose_config")
            return stack_module.CommandResult(0, "", "")
        if "up" in command:
            operations.append("compose_up")
            return stack_module.CommandResult(0, "", "")
        operations.append("compose_ps")
        return stack_module.CommandResult(0, _healthy_ps_output(), "")

    stack_module.stack_up(stack_context, runner=runner)

    assert operations[:7] == [
        "validate_project",
        "check_compose",
        "check_engine",
        "validate_ports",
        "validate_secrets",
        "validate_lock",
        "compose_config",
    ]
    assert operations.index("compose_up") > operations.index("compose_config")


def test_up_validates_project_before_deadline(stack_context: Any) -> None:
    context = stack_module.StackContext(
        stack_context.paths,
        "unsafe-project",
        stack_context.ports,
        stack_context.environment,
    )

    with pytest.raises(StackFailure) as raised:
        stack_module.stack_up(
            context,
            runner=lambda *args, **kwargs: pytest.fail("runner must not be called"),
            deadline_seconds=0,
        )

    assert raised.value.exit_code is StackExitCode.CLI
    assert str(raised.value) == "invalid_project_name"


def test_up_missing_secrets_checks_exact_project_volumes_before_mutation(
    monkeypatch: pytest.MonkeyPatch, stack_context: Any
) -> None:
    calls: list[tuple[tuple[str, ...], float, dict[str, str]]] = []
    monkeypatch.setattr(stack_module, "validate_port_availability", lambda ports: None)
    for secret_file in stack_context.paths.secret_directory.iterdir():
        secret_file.unlink()
    stack_context.paths.secret_directory.rmdir()

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        command = tuple(arguments)
        calls.append((command, timeout_seconds, dict(environment or {})))
        if command[:2] == ("docker", "compose") and command[-2:] == ("version", "--short"):
            return stack_module.CommandResult(0, "2.30.0", "")
        if command[:3] == ("docker", "version", "--format"):
            return stack_module.CommandResult(0, "linux/amd64", "")
        if command[:3] == ("docker", "volume", "ls"):
            return stack_module.CommandResult(0, "knowledge-local_postgres-data\n", "")
        pytest.fail(f"unexpected command boundary: {command[:3]}")

    with pytest.raises(StackFailure) as raised:
        stack_module.stack_up(stack_context, runner=runner)

    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "secret_set_missing_with_volumes"
    volume_call = next(
        command for command, _, _ in calls if command[:3] == ("docker", "volume", "ls")
    )
    assert volume_call == (
        "docker",
        "volume",
        "ls",
        "--quiet",
        "--filter",
        "label=com.docker.compose.project=knowledge-local",
    )
    assert not any("up" in command or "down" in command for command, _, _ in calls)


def test_up_uses_bounded_wait_flags_and_outer_deadline(
    monkeypatch: pytest.MonkeyPatch, stack_context: Any
) -> None:
    calls: list[tuple[tuple[str, ...], float, dict[str, str]]] = []
    monkeypatch.setattr(stack_module, "validate_port_availability", lambda ports: None)

    stack_module.stack_up(stack_context, runner=_successful_lifecycle_runner(calls))

    up_call = next(call for call in calls if "up" in call[0])
    assert up_call[0] == (
        *stack_module.compose_arguments(stack_context),
        "up",
        "--detach",
        "--remove-orphans",
        "--wait",
        "--wait-timeout",
        "180",
    )
    assert 0 < up_call[1] <= 180
    ps_call = next(call for call in calls if "ps" in call[0])
    assert 0 < ps_call[1] <= up_call[1]


def test_up_failure_maps_to_startup_without_automatic_down(
    monkeypatch: pytest.MonkeyPatch, stack_context: Any
) -> None:
    calls: list[tuple[tuple[str, ...], float, dict[str, str]]] = []
    monkeypatch.setattr(stack_module, "validate_port_availability", lambda ports: None)

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        command = tuple(arguments)
        calls.append((command, timeout_seconds, dict(environment or {})))
        if command[:2] == ("docker", "compose") and command[-2:] == ("version", "--short"):
            return stack_module.CommandResult(0, "2.30.0", "")
        if command[:3] == ("docker", "version", "--format"):
            return stack_module.CommandResult(0, "linux/amd64", "")
        if "up" in command:
            return stack_module.CommandResult(1, "raw startup output", "secret startup error")
        return stack_module.CommandResult(0, "", "")

    with pytest.raises(StackFailure) as raised:
        stack_module.stack_up(stack_context, runner=runner)

    assert raised.value.exit_code is StackExitCode.STARTUP
    assert str(raised.value) == "stack_startup_failed"
    assert not any("down" in call[0] or "volume" in call[0] for call in calls)


def test_wait_timeout_returns_temporary_without_down_or_reset(stack_context: Any) -> None:
    calls: list[tuple[tuple[str, ...], float, dict[str, str]]] = []
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(StackFailure) as raised:
        stack_module.wait_for_stack(
            stack_context,
            runner=_successful_lifecycle_runner(calls, ps_output=_starting_ps_output()),
            deadline_seconds=2,
            poll_interval_seconds=0.5,
            clock=clock,
            sleep=sleep,
        )

    assert raised.value.exit_code is StackExitCode.READINESS
    assert str(raised.value) == "stack_readiness_timeout"
    assert not any("down" in call[0] or "volume" in call[0] for call in calls)
    assert len(calls) <= 4


def test_init_nonzero_maps_to_startup(stack_context: Any) -> None:
    rows = json.loads(_healthy_ps_output())
    failed_initializer = next(row for row in rows if row["Service"] == "temporal-schema-setup")
    failed_initializer["ExitCode"] = 17

    with pytest.raises(StackFailure) as raised:
        stack_module.wait_for_stack(
            stack_context,
            runner=_successful_lifecycle_runner(ps_output=json.dumps(rows)),
            deadline_seconds=1,
        )

    assert raised.value.exit_code is StackExitCode.STARTUP
    assert str(raised.value) == "initializer_failed"
    assert "17" not in str(raised.value)


def test_status_output_has_stable_non_secret_shape(stack_context: Any) -> None:
    status = stack_module.stack_status(
        stack_context,
        runner=_successful_lifecycle_runner(ps_output=_healthy_ps_output()),
    )

    assert set(status) == {"project", "state", "services", "initializers", "result_code"}
    assert status["project"] == "knowledge-local"
    assert status["state"] == "ready"
    assert status["result_code"] == "stack_ready"
    assert list(status["services"]) == sorted(_RUNTIME_SERVICES)
    assert list(status["initializers"]) == sorted(_INITIALIZER_SERVICES)
    assert "password" not in json.dumps(status).lower()


def test_status_absence_has_explicit_stable_mapping(stack_context: Any) -> None:
    status = stack_module.stack_status(
        stack_context,
        runner=_successful_lifecycle_runner(ps_output="[]"),
    )

    assert status == {
        "project": "knowledge-local",
        "state": "absent",
        "services": {},
        "initializers": {},
        "result_code": "stack_absent",
    }


def test_status_drops_raw_compose_fields_and_unknown_values(stack_context: Any) -> None:
    raw_secret = "password=must-not-escape"
    rows = json.loads(_healthy_ps_output())
    rows[0].update(
        {
            "ID": raw_secret,
            "Image": raw_secret,
            "Command": raw_secret,
            "Mounts": raw_secret,
            "State": raw_secret,
            "Health": raw_secret,
        }
    )

    status = stack_module.stack_status(
        stack_context,
        runner=_successful_lifecycle_runner(ps_output=json.dumps(rows)),
    )
    rendered = json.dumps(status)

    assert raw_secret not in rendered
    assert "Image" not in rendered
    assert "Command" not in rendered
    assert status["services"]["neo4j"] == {"state": "unknown", "health": "unknown"}


def test_status_is_degraded_when_one_runtime_service_has_stopped(stack_context: Any) -> None:
    rows = json.loads(_healthy_ps_output())
    stopped_service = next(row for row in rows if row["Service"] == "redis")
    stopped_service.update({"State": "exited", "Health": ""})

    status = stack_module.stack_status(
        stack_context,
        runner=_successful_lifecycle_runner(ps_output=json.dumps(rows)),
    )

    assert status["state"] == "degraded"
    assert status["result_code"] == "stack_degraded"


def test_status_rejects_duplicate_service_rows(stack_context: Any) -> None:
    rows = json.loads(_healthy_ps_output())
    rows.append(dict(rows[0]))

    with pytest.raises(StackFailure) as raised:
        stack_module.stack_status(
            stack_context,
            runner=_successful_lifecycle_runner(ps_output=json.dumps(rows)),
        )

    assert raised.value.exit_code is StackExitCode.READINESS
    assert str(raised.value) == "stack_status_invalid"


def test_malformed_status_never_echoes_raw_output(stack_context: Any) -> None:
    raw_secret = "password=must-not-escape"

    with pytest.raises(StackFailure) as raised:
        stack_module.stack_status(
            stack_context,
            runner=_successful_lifecycle_runner(ps_output=raw_secret),
        )

    assert raised.value.exit_code is StackExitCode.READINESS
    assert str(raised.value) == "stack_status_invalid"
    assert raw_secret not in str(raised.value)


def test_lifecycle_never_forwards_r2_environment(
    monkeypatch: pytest.MonkeyPatch, stack_context: Any
) -> None:
    calls: list[tuple[tuple[str, ...], float, dict[str, str]]] = []
    monkeypatch.setattr(stack_module, "validate_port_availability", lambda ports: None)

    stack_module.stack_up(stack_context, runner=_successful_lifecycle_runner(calls))

    assert all("R2_SECRET_ACCESS_KEY" not in environment for _, _, environment in calls)
    assert all("must-not-be-forwarded" not in environment.values() for _, _, environment in calls)


def test_down_never_removes_volumes_images_secrets_or_health_tools(
    stack_context: Any,
) -> None:
    calls: list[tuple[tuple[str, ...], float, dict[str, str]]] = []
    secret_before = {
        path.name: path.read_bytes() for path in stack_context.paths.secret_directory.iterdir()
    }

    stack_module.stack_down(stack_context, runner=_successful_lifecycle_runner(calls))

    assert calls[-1][0] == (
        *stack_module.compose_arguments(stack_context),
        "down",
        "--remove-orphans",
        "--timeout",
        "30",
    )
    flat = " ".join(part for command, _, _ in calls for part in command)
    assert "--volumes" not in flat
    assert "--rmi" not in flat
    assert not any(command[:2] == ("docker", "volume") for command, _, _ in calls)
    assert "temporal-health-tools" not in flat
    assert {
        path.name: path.read_bytes() for path in stack_context.paths.secret_directory.iterdir()
    } == secret_before
