from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).parents[3]))

from tools import local_service_stack as stack_module
from tools.local_service_stack import (
    SECRET_SPECS,
    SecretSetState,
    SmokeMarkerSet,
    StackContext,
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


def test_redis_acl_credential_matches_application_password(tmp_path: Path) -> None:
    paths = resolve_stack_paths(tmp_path)
    generation_index = 0

    def distinct_random_bytes(count: int) -> bytes:
        nonlocal generation_index
        generated = bytes((offset + generation_index) % 256 for offset in range(count))
        generation_index += 1
        return generated

    bootstrap_secret_set(paths, random_bytes=distinct_random_bytes)
    redis_acl = (paths.secret_directory / "redis_acl").read_text(encoding="ascii")
    redis_password = (paths.secret_directory / "redis_application_password").read_text(
        encoding="ascii"
    )
    redis_prefix = "user default off\nuser knowledge on >"
    redis_suffix = " ~* +@all\n"
    has_matching_redis_credential = (
        redis_acl[len(redis_prefix) : -len(redis_suffix)] == redis_password
    )

    assert has_matching_redis_credential


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
    monkeypatch.setattr(stack_module, "_is_current_windows_user_owner", lambda path: True)

    assert inspect_secret_set(paths) is SecretSetState.COMPLETE


def test_complete_stack_secret_set_allows_documented_application_secret_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = resolve_stack_paths(tmp_path)
    bootstrap_secret_set(paths)
    for filename in (
        "auth-key-2026-08.key",
        "policy_signing_a.pem",
        "policy_signing_b.pem",
        "r2_access_key_id",
        "r2_secret_access_key",
        "web-credential-password.key",
    ):
        (paths.secret_directory / filename).write_text("application-secret", encoding="ascii")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(stack_module, "_is_current_windows_user_owner", lambda path: True)

    assert inspect_secret_set(paths) is SecretSetState.COMPLETE


@pytest.mark.parametrize(
    ("environment", "configured_relative_paths"),
    [
        (
            {"KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "auth-key-2026-09.key"},
            ("auth-key-2026-09.key",),
        ),
        (
            {
                "KNOWLEDGE_AUTH_PREVIOUS_KEYS": (
                    "auth-key-2026-07=auth-key-2026-07.key,"
                    "auth-key-2026-08=authentication/2026-08.key"
                )
            },
            ("auth-key-2026-07.key", "authentication/2026-08.key"),
        ),
        (
            {"KNOWLEDGE_POLICY_SIGNING_KEY_FILE": "policy/rotation-2026_09.pem"},
            ("policy/rotation-2026_09.pem",),
        ),
    ],
)
def test_configured_runtime_secret_survives_inspection_reset_and_rebootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment: dict[str, str],
    configured_relative_paths: tuple[str, ...],
) -> None:
    paths = resolve_stack_paths(tmp_path)
    bootstrap_secret_set(paths, environment=environment)
    application_secrets = tuple(
        paths.secret_directory.joinpath(*relative_path.split("/"))
        for relative_path in configured_relative_paths
    )
    for application_secret in application_secrets:
        application_secret.parent.mkdir(parents=True, exist_ok=True)
        application_secret.write_text("application-secret", encoding="ascii")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(stack_module, "_is_current_windows_user_owner", lambda path: True)

    assert inspect_secret_set(paths, environment=environment) is SecretSetState.COMPLETE
    assert remove_secret_set_after_reset(paths, environment=environment) is SecretSetState.MISSING
    assert all(application_secret.exists() for application_secret in application_secrets)
    assert bootstrap_secret_set(paths, environment=environment) is SecretSetState.COMPLETE
    assert all(application_secret.exists() for application_secret in application_secrets)


@pytest.mark.parametrize(
    "environment",
    [
        {"KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "../outside.key"},
        {"KNOWLEDGE_AUTH_PREVIOUS_KEYS": "old=authentication/../../outside.key"},
        {"KNOWLEDGE_POLICY_SIGNING_KEY_FILE": "C:\\outside.pem"},
    ],
)
def test_runtime_secret_allowlist_rejects_unsafe_paths_without_echoing_values(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    paths = resolve_stack_paths(tmp_path)
    configured_value = next(iter(environment.values()))

    with pytest.raises(StackFailure) as raised:
        inspect_secret_set(paths, environment=environment)

    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "application_secret_configuration_invalid"
    assert configured_value not in str(raised.value)


def test_runtime_secret_allowlist_rejects_previous_file_collision_with_current(
    tmp_path: Path,
) -> None:
    paths = resolve_stack_paths(tmp_path)
    colliding_filename = "authentication/rotation-2026-09.key"
    environment = {
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE": colliding_filename,
        "KNOWLEDGE_AUTH_PREVIOUS_KEYS": f"auth-key-previous={colliding_filename}",
        "KNOWLEDGE_POLICY_SIGNING_KEY_FILE": "policy/rotation-2026-09.pem",
    }

    with pytest.raises(StackFailure) as raised:
        inspect_secret_set(paths, environment=environment)

    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "application_secret_configuration_invalid"
    assert colliding_filename not in str(raised.value)


def test_windows_secret_path_identity_rejects_case_variant_of_managed_file_before_lifecycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inspection/bootstrap/reset must never alias-delete an application secret."""

    paths = resolve_stack_paths(tmp_path)
    bootstrap_secret_set(paths, environment={})
    managed_secret = paths.secret_directory / "postgres_admin_password"
    original_bytes = managed_secret.read_bytes()
    environment = {"KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "POSTGRES_ADMIN_PASSWORD"}
    monkeypatch.setattr(sys, "platform", "win32")

    for lifecycle_operation in (
        inspect_secret_set,
        bootstrap_secret_set,
        remove_secret_set_after_reset,
    ):
        with pytest.raises(StackFailure) as raised:
            lifecycle_operation(paths, environment=environment)
        assert raised.value.exit_code is StackExitCode.CONTRACT
        assert str(raised.value) == "application_secret_configuration_invalid"
        assert managed_secret.read_bytes() == original_bytes


def test_windows_secret_path_identity_rejects_current_previous_case_variant_collision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = resolve_stack_paths(tmp_path)
    environment = {
        "KNOWLEDGE_AUTH_CURRENT_KEY_ID": "auth-key-current",
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "Authentication/Current.key",
        "KNOWLEDGE_AUTH_PREVIOUS_KEYS": ("auth-key-previous=authentication/current.KEY"),
    }
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(StackFailure) as raised:
        inspect_secret_set(paths, environment=environment)

    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "application_secret_configuration_invalid"


def test_windows_secret_path_identity_does_not_casefold_authentication_key_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = resolve_stack_paths(tmp_path)
    environment = {
        "KNOWLEDGE_AUTH_CURRENT_KEY_ID": "Auth-Key-Current",
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "authentication/current.key",
        "KNOWLEDGE_AUTH_PREVIOUS_KEYS": ("auth-key-current=authentication/previous.key"),
    }
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(StackFailure) as raised:
        inspect_secret_set(paths, environment=environment)

    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "application_secret_configuration_invalid"


@pytest.mark.parametrize(
    "authentication_environment",
    [
        {
            "KNOWLEDGE_AUTH_CURRENT_KEY_ID": "auth-key-current",
            "KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "Keys/Shared.key",
        },
        {
            "KNOWLEDGE_AUTH_PREVIOUS_KEYS": "auth-key-previous=Keys/Shared.key",
        },
    ],
)
def test_windows_secret_path_identity_rejects_policy_authentication_case_variant_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    authentication_environment: dict[str, str],
) -> None:
    paths = resolve_stack_paths(tmp_path)
    environment = {
        **authentication_environment,
        "KNOWLEDGE_POLICY_SIGNING_KEY_FILE": "keys/shared.KEY",
    }
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(StackFailure) as raised:
        inspect_secret_set(paths, environment=environment)

    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "application_secret_configuration_invalid"


def test_remove_managed_secret_set_preserves_shared_application_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = resolve_stack_paths(tmp_path)
    bootstrap_secret_set(paths)
    application_secret = paths.secret_directory / "r2_access_key_id"
    application_secret.write_text("application-secret", encoding="ascii")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(stack_module, "_is_current_windows_user_owner", lambda path: True)

    assert remove_secret_set_after_reset(paths) is SecretSetState.MISSING
    assert application_secret.exists()
    assert all(not (paths.secret_directory / spec.filename).exists() for spec in SECRET_SPECS)
    assert inspect_secret_set(paths) is SecretSetState.MISSING


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
        (
            "redis_acl",
            "user default off\nuser knowledge on >ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef ~* +@all\n",
        ),
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


def test_port_availability_probe_sets_reuseaddr_before_binding_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe like Docker binds: with SO_REUSEADDR.

    Docker publishes the stack ports with SO_REUSEADDR, so a probe without it
    reports port_unavailable against TIME_WAIT sockets left on the very same
    loopback ports by a torn-down stack cycle.
    """

    monkeypatch.setattr(stack_module.sys, "platform", "linux")
    tracked = _TrackedSocket(bind_fails=False)

    validate_port_availability({"POSTGRES_PORT": 5432}, socket_factory=lambda family, kind: tracked)

    assert tracked.options == [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]
    assert tracked.closed


def test_subprocess_environment_omits_credentials_and_r2() -> None:
    clean = sanitize_subprocess_environment(
        {
            "PATH": "safe",
            "R2_SECRET_ACCESS_KEY": "secret",
            "POSTGRES_PASSWORD": "secret",
            "KNOWLEDGE_AUTH_CURRENT_KEY_ID": "auth-key-current",
            "KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "authentication/current.key",
            "KNOWLEDGE_AUTH_PREVIOUS_KEYS": "auth-key-previous=authentication/previous.key",
            "KNOWLEDGE_POLICY_SIGNING_KEY_FILE": "policy/current.pem",
        }
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
            raise subprocess.TimeoutExpired(["blocked-reader"], timeout or 0.0)
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


def test_stack_failure_propagates_cleanly_through_context_managers() -> None:
    """Python 3.14 regression: frozen exceptions must survive `with` blocks.

    The context machinery assigns ``__traceback__`` (and friends) while an
    exception propagates through a generator context manager; a plain frozen
    dataclass turns that bookkeeping into ``FrozenInstanceError`` and the
    closed failure itself is lost. Raised inside ``with`` blocks on a loaded
    machine, ``StackFailure`` must still arrive as ``StackFailure``.
    """

    @contextmanager
    def probe() -> Iterator[None]:
        yield

    with pytest.raises(StackFailure, match="invalid_command"), probe():
        raise StackFailure(StackExitCode.CLI, "invalid_command")


def test_stack_failure_still_rejects_field_mutation() -> None:
    failure = StackFailure(StackExitCode.CLI, "invalid_command")

    with pytest.raises(FrozenInstanceError):
        failure.result_code = "mutated"


def test_run_command_retries_one_transient_spawn_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single transient Windows spawn refusal must not fail the command.

    Under heavy parallel load the OS can refuse one ``Popen`` (pipe-buffer
    pressure); the bounded single retry keeps the capture contract while a
    persistent outage still surfaces as ``subprocess_unavailable`` after
    exactly two attempts.
    """

    real_popen = subprocess.Popen
    spawn_attempts: list[int] = []

    def flaky_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        spawn_attempts.append(1)
        if len(spawn_attempts) == 1:
            raise OSError("transient pipe-buffer pressure")
        # String cast: the subscript must not evaluate at runtime, where
        # subprocess.Popen is monkeypatched to this plain function.
        return cast("subprocess.Popen[bytes]", real_popen(*args, **kwargs))

    monkeypatch.setattr(subprocess, "Popen", flaky_popen)
    result = run_command(
        [sys.executable, "-c", "print('recovered')"],
        timeout_seconds=5.0,
    )

    assert result.return_code == 0
    assert "recovered" in result.stdout
    assert len(spawn_attempts) == 2


def test_run_command_surfaces_subprocess_unavailable_after_two_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refusing_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        raise OSError("persistent")

    monkeypatch.setattr(subprocess, "Popen", refusing_popen)
    with pytest.raises(StackFailure, match="subprocess_unavailable"):
        run_command([sys.executable, "-c", "print('never')"], timeout_seconds=5.0)


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
        probe_service = _semantic_probe_service(command)
        if probe_service is not None:
            return stack_module.CommandResult(
                0,
                f"{probe_service.replace('-', '_')}_contract_ready\n",
                "",
            )
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
        context.project_name = "knowledge-ci-mutated"  # type: ignore[misc]
    with pytest.raises(TypeError):
        cast(dict[str, int], context.ports)["POSTGRES_PORT"] = 35432


def test_stack_context_retains_runtime_secret_references_for_status_without_forwarding(
    stack_context: Any,
) -> None:
    reference_environment = {
        "PATH": "safe",
        "KNOWLEDGE_AUTH_CURRENT_KEY_ID": "auth-key-current",
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "authentication/current.key",
        "KNOWLEDGE_AUTH_PREVIOUS_KEYS": ("auth-key-previous=authentication/previous.key"),
        "KNOWLEDGE_POLICY_SIGNING_KEY_FILE": "policy/current.pem",
    }
    context = stack_module.StackContext(
        stack_context.paths,
        stack_context.project_name,
        stack_context.ports,
        reference_environment,
    )
    calls: list[tuple[tuple[str, ...], float, dict[str, str]]] = []

    status = stack_module.stack_status(
        context,
        runner=_successful_lifecycle_runner(calls, ps_output=_healthy_ps_output()),
    )

    assert status["state"] == "ready"
    assert context.environment == {"PATH": "safe"}
    assert context.application_secret_references.current_authentication_key_id == (
        "auth-key-current"
    )
    assert context.application_secret_references.current_authentication_key_file == (
        "authentication/current.key"
    )
    assert context.application_secret_references.previous_authentication_keys == (
        ("auth-key-previous", "authentication/previous.key"),
    )
    assert context.application_secret_references.policy_signing_key_file == "policy/current.pem"
    assert calls
    assert all(
        not any(
            environment_name.startswith("KNOWLEDGE_AUTH_")
            or environment_name == "KNOWLEDGE_POLICY_SIGNING_KEY_FILE"
            for environment_name in forwarded_environment
        )
        for _command, _timeout_seconds, forwarded_environment in calls
    )


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


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2.30.0", (2, 30, 0)),
        ("2.30.0+desktop.1", (2, 30, 0)),
        ("2.31.0-rc.1", (2, 31, 0)),
    ],
)
def test_prerequisites_accept_stable_minimum_and_newer_versions(
    stack_context: Any, version: str, expected: tuple[int, int, int]
) -> None:
    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        return stack_module.CommandResult(0, version, "")

    versions = stack_module.check_prerequisites(
        stack_context,
        runner=runner,
        require_engine=False,
    )

    assert versions == stack_module.PrerequisiteVersions(expected, "", "")


@pytest.mark.parametrize(
    "version",
    ["2.30.0-rc.1", "v2.30.0-beta.2", "Docker Compose version v2.30.0-alpha"],
)
def test_prerequisites_reject_compose_2_30_0_prerelease(stack_context: Any, version: str) -> None:
    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        return stack_module.CommandResult(0, version, "")

    with pytest.raises(StackFailure) as raised:
        stack_module.check_prerequisites(stack_context, runner=runner, require_engine=False)

    assert raised.value.exit_code is StackExitCode.PREREQUISITE
    assert str(raised.value) == "compose_version_unsupported"


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

    def validate_secrets(
        paths: Any,
        *,
        list_project_volumes: Any,
        application_secret_references: Any = None,
    ) -> Any:
        assert application_secret_references == stack_context.application_secret_references
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
        probe_service = _semantic_probe_service(command)
        if probe_service is not None:
            operations.append(f"probe_{probe_service}")
            return stack_module.CommandResult(
                0,
                f"{probe_service.replace('-', '_')}_contract_ready\n",
                "",
            )
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
    services = status["services"]
    initializers = status["initializers"]
    assert isinstance(services, dict)
    assert isinstance(initializers, dict)
    assert list(services) == sorted(_RUNTIME_SERVICES)
    assert list(initializers) == sorted(_INITIALIZER_SERVICES)
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
    services = status["services"]
    assert isinstance(services, dict)
    assert services["neo4j"] == {"state": "unknown", "health": "unknown"}


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


def test_status_projects_stable_fields_before_bounded_capture(stack_context: Any) -> None:
    calls: list[tuple[str, ...]] = []
    expected_template = (
        '{"Service":{{json .Service}},"State":{{json .State}},'
        '"Health":{{json .Health}},"ExitCode":{{json .ExitCode}}}'
    )

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        command = tuple(arguments)
        calls.append(command)
        if command[-2:] == ("version", "--short"):
            return stack_module.CommandResult(0, "2.30.0", "")
        if command[:3] == ("docker", "version", "--format"):
            return stack_module.CommandResult(0, "linux/amd64", "")
        if "ps" in command and command[-1] == expected_template:
            return stack_module.CommandResult(0, _healthy_ps_output(), "")
        if "ps" in command:
            return stack_module.CommandResult(
                0,
                '{"Service":"postgresql","Command":"' + ("x" * 8150),
                "",
            )
        pytest.fail(f"unexpected status command: {command}")

    status = stack_module.stack_status(stack_context, runner=runner)

    assert status["state"] == "ready"
    ps_call = next(command for command in calls if "ps" in command)
    assert ps_call[-2:] == ("--format", expected_template)


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


_EXPECTED_RESET_VOLUMES = {
    "knowledge-local_postgres-data",
    "knowledge-local_qdrant-data",
    "knowledge-local_neo4j-data",
    "knowledge-local_redis-data",
    "knowledge-local_temporal-health-tools",
}


def _semantic_probe_service(command: tuple[str, ...]) -> str | None:
    if "exec" in command and "--no-TTY" in command:
        container_service = command[command.index("--no-TTY") + 1]
        if container_service == "temporal-cli" and "temporal_contract_ready" in command[-1]:
            return "temporal"
        return container_service
    if command and command[0] == sys.executable and "temporal-ui" in command:
        return "temporal-ui"
    return None


def _semantic_probe_runner(
    *, failing_service: str | None = None, raw_output: str = "", return_code: int = 1
) -> Any:
    def run(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        command = tuple(arguments)
        if command[:2] == ("docker", "compose") and command[-2:] == ("version", "--short"):
            return stack_module.CommandResult(0, "2.30.0\n", "")
        if command[:3] == ("docker", "version", "--format"):
            return stack_module.CommandResult(0, "linux/amd64\n", "")
        if "ps" in command:
            return stack_module.CommandResult(0, _healthy_ps_output(), "")
        if command[:2] == ("docker", "compose") and any(
            operation in command for operation in ("config", "up", "down")
        ):
            return stack_module.CommandResult(0, "", "")
        service = _semantic_probe_service(command)
        if service is None:
            pytest.fail(f"unexpected semantic command: {command}")
        if service == failing_service:
            return stack_module.CommandResult(return_code, raw_output, raw_output)
        return stack_module.CommandResult(
            0,
            f"{service.replace('-', '_')}_contract_ready\n",
            "",
        )

    return run


def test_verify_returns_only_fixed_redacted_probe_results(stack_context: Any) -> None:
    results = stack_module.verify_stack(
        stack_context,
        runner=_semantic_probe_runner(raw_output="DO_NOT_LEAK"),
    )

    assert [result.service for result in results] == [
        "postgresql",
        "qdrant",
        "neo4j",
        "redis",
        "temporal",
        "temporal-ui",
    ]
    assert all(result.is_ready for result in results)
    assert all(result.result_code.endswith("_contract_ready") for result in results)
    assert all(type(result.latency_ms) is int and result.latency_ms >= 0 for result in results)
    assert all(
        set(result.__dataclass_fields__)
        == {
            "service",
            "is_ready",
            "result_code",
            "latency_ms",
        }
        for result in results
    )
    assert "DO_NOT_LEAK" not in repr(results)


@pytest.mark.parametrize(
    ("probe", "expected_code"),
    [
        ("postgresql", "postgresql_contract_failed"),
        ("qdrant", "qdrant_contract_failed"),
        ("neo4j", "neo4j_contract_failed"),
        ("redis", "redis_contract_failed"),
        ("temporal", "temporal_contract_failed"),
        ("temporal-ui", "temporal_ui_contract_failed"),
    ],
)
def test_verify_maps_each_probe_to_fixed_redacted_code(
    stack_context: Any, probe: str, expected_code: str
) -> None:
    with pytest.raises(StackFailure) as raised:
        stack_module.verify_stack(
            stack_context,
            runner=_semantic_probe_runner(failing_service=probe, raw_output="DO_NOT_LEAK"),
        )

    assert raised.value.exit_code is StackExitCode.READINESS
    assert str(raised.value) == expected_code
    assert "DO_NOT_LEAK" not in str(raised.value)


def test_verify_maps_explicit_probe_contract_drift_to_contract(stack_context: Any) -> None:
    with pytest.raises(StackFailure) as raised:
        stack_module.verify_stack(
            stack_context,
            runner=_semantic_probe_runner(failing_service="redis", return_code=65),
        )

    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "redis_contract_failed"


def test_verify_uses_argument_arrays_and_bounded_probe_deadlines(stack_context: Any) -> None:
    calls: list[tuple[tuple[str, ...], float]] = []
    base_runner = _semantic_probe_runner()

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        calls.append((tuple(arguments), timeout_seconds))
        return base_runner(
            arguments,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )

    stack_module.verify_stack(stack_context, runner=runner)

    probe_calls = [call for call in calls if _semantic_probe_service(call[0]) is not None]
    assert len(probe_calls) == 6
    assert all(0 < timeout_seconds <= 10 for _, timeout_seconds in probe_calls)
    compose_exec_calls = [
        command for command, _ in probe_calls if command[:2] == ("docker", "compose")
    ]
    assert all("exec" in command and "--no-TTY" in command for command in compose_exec_calls)
    assert all("DO_NOT_LEAK" not in part for command, _ in probe_calls for part in command)


def test_qdrant_probe_constructs_authenticated_header_with_real_crlf(stack_context: Any) -> None:
    base_runner = _semantic_probe_runner()

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        command = tuple(arguments)
        if _semantic_probe_service(command) == "qdrant":
            probe_script = command[-1]
            if "api-key: %s\\r\\nConnection: close" not in probe_script:
                return stack_module.CommandResult(75, "", "")
        return base_runner(
            arguments,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )

    results = stack_module.verify_stack(stack_context, runner=runner)

    assert next(result for result in results if result.service == "qdrant").is_ready


def test_postgresql_probe_authenticates_each_principal_and_denies_cross_database_access(
    stack_context: Any,
) -> None:
    arguments, _ = stack_module._semantic_probe_arguments(stack_context, "postgresql")
    probe_script = arguments[-1]

    assert "cat /run/secrets/postgres_admin_password" in probe_script
    assert "cat /run/secrets/postgres_application_password" in probe_script
    assert "cat /run/secrets/postgres_temporal_password" in probe_script
    assert "--username knowledge_app --dbname knowledge" in probe_script
    assert "--username temporal_service --dbname temporal" in probe_script
    assert "--username temporal_service --dbname temporal_visibility" in probe_script
    assert "--username knowledge_app --dbname temporal" in probe_script
    assert "--username knowledge_app --dbname temporal_visibility" in probe_script
    assert "--username temporal_service --dbname knowledge" in probe_script
    assert probe_script.count("then exit 65; fi") >= 3
    assert "unset admin_password application_password temporal_password" in probe_script


def test_neo4j_probe_requires_invalid_credential_rejection(stack_context: Any) -> None:
    arguments, _ = stack_module._semantic_probe_arguments(stack_context, "neo4j")
    probe_script = arguments[-1]

    assert "if cypher-shell" in probe_script
    assert "--password invalid-probe-credential" in probe_script
    assert ">/dev/null 2>&1; then exit 65; fi" in probe_script


def test_temporal_probe_executes_in_pinned_cli_toolbox(stack_context: Any) -> None:
    arguments, success_code = stack_module._semantic_probe_arguments(stack_context, "temporal")

    assert success_code == "temporal_contract_ready"
    assert arguments[arguments.index("--no-TTY") + 1] == "temporal-cli"


def test_temporal_probe_accepts_registered_namespace_enum(stack_context: Any) -> None:
    base_runner = _semantic_probe_runner()

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        command = tuple(arguments)
        if _semantic_probe_service(command) == "temporal":
            probe_script = command[-1]
            if "(NAMESPACE_STATE_)?REGISTERED" not in probe_script:
                return stack_module.CommandResult(65, "", "")
        return base_runner(
            arguments,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )

    results = stack_module.verify_stack(stack_context, runner=runner)

    assert next(result for result in results if result.service == "temporal").is_ready


def test_temporal_ui_http_error_is_contract_drift_not_transport_failure(
    stack_context: Any,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    ui_port = listener.getsockname()[1]

    def respond_not_found() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.recv(4096)
            connection.sendall(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )

    responder = threading.Thread(target=respond_not_found, daemon=True)
    responder.start()
    context = stack_module.StackContext(
        stack_context.paths,
        stack_context.project_name,
        {**stack_context.ports, "TEMPORAL_UI_PORT": ui_port},
        stack_context.environment,
    )
    arguments, _ = stack_module._semantic_probe_arguments(context, "temporal-ui")
    try:
        result = run_command(arguments, timeout_seconds=3.0)
    finally:
        listener.close()
        responder.join(timeout=1.0)

    assert result.return_code == 65
    assert result.stdout == ""


def test_verify_absent_stack_is_cli_failure(stack_context: Any) -> None:
    calls: list[tuple[str, ...]] = []
    base_runner = _successful_lifecycle_runner(ps_output="[]")

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        calls.append(tuple(arguments))
        return base_runner(
            arguments,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )

    with pytest.raises(StackFailure) as raised:
        stack_module.verify_stack(stack_context, runner=runner)

    assert raised.value.exit_code is StackExitCode.CLI
    assert str(raised.value) == "stack_absent"
    assert not any(_semantic_probe_service(command) is not None for command in calls)


def test_up_runs_semantic_verification_after_container_readiness(
    monkeypatch: pytest.MonkeyPatch, stack_context: Any
) -> None:
    calls: list[tuple[str, ...]] = []
    base_runner = _semantic_probe_runner()
    monkeypatch.setattr(stack_module, "validate_port_availability", lambda ports: None)

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        calls.append(tuple(arguments))
        return base_runner(
            arguments,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )

    stack_module.stack_up(stack_context, runner=runner)

    first_probe_index = next(
        index for index, command in enumerate(calls) if _semantic_probe_service(command) is not None
    )
    readiness_index = max(index for index, command in enumerate(calls) if "ps" in command)
    assert first_probe_index > readiness_index


def test_up_caps_semantic_verification_to_thirty_second_aggregate_deadline(
    monkeypatch: pytest.MonkeyPatch, stack_context: Any
) -> None:
    observed_deadlines: list[float] = []
    monkeypatch.setattr(stack_module, "validate_port_availability", lambda ports: None)
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)

    def probes(
        context: Any,
        *,
        runner: Any,
        deadline_monotonic: float,
        clock: Any,
    ) -> tuple[Any, ...]:
        del context, runner, clock
        observed_deadlines.append(deadline_monotonic)
        return ()

    monkeypatch.setattr(stack_module, "_run_semantic_probes", probes)

    stack_module.stack_up(
        stack_context,
        runner=_successful_lifecycle_runner(),
        deadline_seconds=180.0,
    )

    assert observed_deadlines == [130.0]


def _reset_runner(
    calls: list[tuple[str, ...]],
    *,
    project_volumes: set[str] | None = None,
    label_volumes: dict[str, set[str]] | None = None,
    fail_remove_index: int | None = None,
    inject_unknown_after_down: bool = False,
) -> Any:
    all_project_volumes = set(
        _EXPECTED_RESET_VOLUMES if project_volumes is None else project_volumes
    )
    resolved_by_label = {
        label: {f"knowledge-local_{label}"}
        for label in (
            "postgres-data",
            "qdrant-data",
            "neo4j-data",
            "redis-data",
            "temporal-health-tools",
        )
    }
    if label_volumes is not None:
        resolved_by_label = {label: set(names) for label, names in label_volumes.items()}
    removed: set[str] = set()
    remove_count = 0
    has_run_down = False

    def run(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        nonlocal has_run_down, remove_count
        command = tuple(arguments)
        calls.append(command)
        if command[:2] == ("docker", "compose") and command[-2:] == ("version", "--short"):
            return stack_module.CommandResult(0, "2.30.0\n", "")
        if command[:3] == ("docker", "version", "--format"):
            return stack_module.CommandResult(0, "linux/amd64\n", "")
        if command[:4] == ("docker", "volume", "ls", "--quiet"):
            volume_label_filter = next(
                (part for part in command if part.startswith("label=com.docker.compose.volume=")),
                None,
            )
            if volume_label_filter is None:
                names = all_project_volumes - removed
                if has_run_down and inject_unknown_after_down:
                    names = {*names, "knowledge-local_concurrent-unknown"}
            else:
                logical_name = volume_label_filter.rsplit("=", maxsplit=1)[1]
                names = resolved_by_label.get(logical_name, set()) - removed
            return stack_module.CommandResult(0, "".join(f"{name}\n" for name in sorted(names)), "")
        if command[:3] == ("docker", "volume", "inspect"):
            volume_name = command[-1]
            logical_names = [
                label for label, names in resolved_by_label.items() if volume_name in names
            ]
            logical_name = logical_names[0] if len(logical_names) == 1 else "unknown"
            return stack_module.CommandResult(
                0,
                f"knowledge-local\t{logical_name}\n",
                "",
            )
        if command[:3] == ("docker", "volume", "rm"):
            remove_count += 1
            if remove_count == fail_remove_index:
                return stack_module.CommandResult(1, "DO_NOT_LEAK", "DO_NOT_LEAK")
            removed.add(command[3])
            return stack_module.CommandResult(0, command[3], "")
        if "down" in command:
            has_run_down = True
            return stack_module.CommandResult(0, "", "")
        pytest.fail(f"unexpected reset command: {command}")

    return run


def test_reset_requires_exact_double_confirmation(stack_context: Any) -> None:
    with pytest.raises(StackFailure) as raised:
        stack_module.reset_stack(
            stack_context,
            confirm_project="knowledge-local-typo",
            runner=lambda *args, **kwargs: pytest.fail("runner must not be called"),
        )

    assert raised.value.exit_code is StackExitCode.CLI
    assert str(raised.value) == "reset_confirmation_mismatch"


def test_reset_deletes_only_exact_labeled_project_volumes(stack_context: Any) -> None:
    calls: list[tuple[str, ...]] = []

    result = stack_module.reset_stack(
        stack_context,
        confirm_project="knowledge-local",
        runner=_reset_runner(calls),
    )

    removed_volumes = {command[3] for command in calls if command[:3] == ("docker", "volume", "rm")}
    assert removed_volumes == _EXPECTED_RESET_VOLUMES
    assert result == {
        "project": "knowledge-local",
        "state": "absent",
        "removed_volumes": 5,
        "secrets": "preserved",
        "result_code": "stack_reset_complete",
    }
    down_index = next(index for index, command in enumerate(calls) if "down" in command)
    first_remove_index = next(
        index for index, command in enumerate(calls) if command[:3] == ("docker", "volume", "rm")
    )
    assert down_index < first_remove_index
    exact_label_calls = [
        command for command in calls[:down_index] if command[:3] == ("docker", "volume", "inspect")
    ]
    assert {command[-1] for command in exact_label_calls} == _EXPECTED_RESET_VOLUMES
    assert all("com.docker.compose.project" in command[-2] for command in exact_label_calls)
    assert all("com.docker.compose.volume" in command[-2] for command in exact_label_calls)
    flat = " ".join(part for command in calls for part in command)
    assert "prune" not in flat
    assert "--force" not in flat


def test_reset_intersects_project_and_volume_labels_without_or_filter_semantics(
    stack_context: Any,
) -> None:
    calls: list[tuple[str, ...]] = []
    base_runner = _reset_runner(calls)

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        command = tuple(arguments)
        label_filters = [part for part in command if part.startswith("label=")]
        if command[:4] == ("docker", "volume", "ls", "--quiet") and len(label_filters) == 2:
            calls.append(command)
            return stack_module.CommandResult(
                0,
                "".join(f"{name}\n" for name in sorted(_EXPECTED_RESET_VOLUMES)),
                "",
            )
        return base_runner(
            arguments,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )

    result = stack_module.reset_stack(
        stack_context,
        confirm_project="knowledge-local",
        runner=runner,
    )

    assert result["removed_volumes"] == 5
    assert not any(
        len([part for part in command if part.startswith("label=")]) > 1 for command in calls
    )


def test_reset_refuses_unknown_labeled_volume_before_down(stack_context: Any) -> None:
    calls: list[tuple[str, ...]] = []
    volumes = {*_EXPECTED_RESET_VOLUMES, "knowledge-local_unexpected-data"}

    with pytest.raises(StackFailure) as raised:
        stack_module.reset_stack(
            stack_context,
            confirm_project="knowledge-local",
            runner=_reset_runner(calls, project_volumes=volumes),
        )

    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "unexpected_project_volume"
    assert not any("down" in command for command in calls)
    assert not any(command[:3] == ("docker", "volume", "rm") for command in calls)


def test_reset_refuses_partial_expected_volume_set_before_down(stack_context: Any) -> None:
    calls: list[tuple[str, ...]] = []
    volumes = _EXPECTED_RESET_VOLUMES - {"knowledge-local_temporal-health-tools"}
    labels = {name.removeprefix("knowledge-local_"): {name} for name in volumes}

    with pytest.raises(StackFailure) as raised:
        stack_module.reset_stack(
            stack_context,
            confirm_project="knowledge-local",
            runner=_reset_runner(calls, project_volumes=volumes, label_volumes=labels),
        )

    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "project_volume_set_incomplete"
    assert not any("down" in command for command in calls)


def test_reset_rechecks_label_set_after_down_before_first_delete(stack_context: Any) -> None:
    calls: list[tuple[str, ...]] = []

    with pytest.raises(StackFailure) as raised:
        stack_module.reset_stack(
            stack_context,
            confirm_project="knowledge-local",
            runner=_reset_runner(calls, inject_unknown_after_down=True),
        )

    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "unexpected_project_volume"
    assert any("down" in command for command in calls)
    assert not any(command[:3] == ("docker", "volume", "rm") for command in calls)


def test_reset_without_volumes_is_idempotent_but_cannot_rotate(
    stack_context: Any,
) -> None:
    calls: list[tuple[str, ...]] = []
    labels: dict[str, set[str]] = {
        label: set()
        for label in (
            "postgres-data",
            "qdrant-data",
            "neo4j-data",
            "redis-data",
            "temporal-health-tools",
        )
    }

    result = stack_module.reset_stack(
        stack_context,
        confirm_project="knowledge-local",
        runner=_reset_runner(calls, project_volumes=set(), label_volumes=labels),
    )

    assert result["removed_volumes"] == 0
    assert result["secrets"] == "preserved"
    with pytest.raises(StackFailure) as raised:
        stack_module.reset_stack(
            stack_context,
            confirm_project="knowledge-local",
            rotate_secrets=True,
            runner=_reset_runner([], project_volumes=set(), label_volumes=labels),
        )
    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "secret_rotation_requires_volume_deletion"


def test_secret_rotation_occurs_only_after_all_volume_deletes_succeed(
    stack_context: Any,
) -> None:
    calls: list[tuple[str, ...]] = []

    with pytest.raises(StackFailure) as raised:
        stack_module.reset_stack(
            stack_context,
            confirm_project="knowledge-local",
            rotate_secrets=True,
            runner=_reset_runner(calls, fail_remove_index=2),
        )

    assert raised.value.exit_code is StackExitCode.STARTUP
    assert str(raised.value) == "volume_removal_failed"
    assert stack_context.paths.secret_directory.exists()
    assert "DO_NOT_LEAK" not in str(raised.value)


def test_secret_rotation_removes_exact_secret_set_after_verified_deletion(
    stack_context: Any,
) -> None:
    calls: list[tuple[str, ...]] = []

    result = stack_module.reset_stack(
        stack_context,
        confirm_project="knowledge-local",
        rotate_secrets=True,
        runner=_reset_runner(calls),
    )

    assert result["secrets"] == "removed"
    assert not stack_context.paths.secret_directory.exists()
    last_remove_index = max(
        index for index, command in enumerate(calls) if command[:3] == ("docker", "volume", "rm")
    )
    assert any(
        command[:4] == ("docker", "volume", "ls", "--quiet")
        for command in calls[last_remove_index + 1 :]
    )


def test_secret_rotation_preserves_application_files_and_rebootstraps_fresh_managed_set(
    stack_context: Any,
) -> None:
    calls: list[tuple[str, ...]] = []
    application_filenames = (
        "auth-key-2026-08.key",
        "policy_signing_a.pem",
        "policy_signing_b.pem",
        "r2_access_key_id",
        "r2_secret_access_key",
        "web-credential-password.key",
    )
    expected_application_contents: dict[str, bytes] = {}
    for index, filename in enumerate(application_filenames):
        content = f"application-secret-{index}".encode("ascii")
        application_path = stack_context.paths.secret_directory / filename
        application_path.write_bytes(content)
        application_path.chmod(0o600)
        expected_application_contents[filename] = content
    original_managed_fingerprint = stack_module._smoke_secret_fingerprint(
        stack_context.paths, stack_context.application_secret_references
    )

    result = stack_module.reset_stack(
        stack_context,
        confirm_project="knowledge-local",
        rotate_secrets=True,
        runner=_reset_runner(calls),
    )

    assert result["secrets"] == "removed"
    assert stack_context.paths.secret_directory.is_dir()
    assert inspect_secret_set(stack_context.paths) is SecretSetState.MISSING
    assert {
        filename: (stack_context.paths.secret_directory / filename).read_bytes()
        for filename in application_filenames
    } == expected_application_contents

    assert (
        bootstrap_secret_set(
            stack_context.paths,
            random_bytes=lambda size: bytes(range(32, 32 + size)),
            environment=stack_context.environment,
        )
        is SecretSetState.COMPLETE
    )
    assert {
        filename: (stack_context.paths.secret_directory / filename).read_bytes()
        for filename in application_filenames
    } == expected_application_contents
    assert (
        stack_module._smoke_secret_fingerprint(
            stack_context.paths,
            stack_context.application_secret_references,
        )
        != original_managed_fingerprint
    )


def test_runtime_configured_secret_references_survive_reset_rotation_and_rebootstrap(
    stack_context: Any,
) -> None:
    reference_environment = {
        "PATH": "safe",
        "KNOWLEDGE_AUTH_CURRENT_KEY_ID": "auth-key-current",
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "authentication/current.key",
        "KNOWLEDGE_AUTH_PREVIOUS_KEYS": ("auth-key-previous=authentication/previous.key"),
        "KNOWLEDGE_POLICY_SIGNING_KEY_FILE": "policy/current.pem",
    }
    context = stack_module.StackContext(
        stack_context.paths,
        stack_context.project_name,
        stack_context.ports,
        reference_environment,
    )
    expected_contents = {
        "authentication/current.key": b"current-application-key",
        "authentication/previous.key": b"previous-application-key",
        "policy/current.pem": b"policy-application-key",
    }
    for relative_path, content in expected_contents.items():
        secret_path = context.paths.secret_directory.joinpath(*relative_path.split("/"))
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_bytes(content)
        secret_path.chmod(0o600)

    result = stack_module.reset_stack(
        context,
        confirm_project="knowledge-local",
        rotate_secrets=True,
        runner=_reset_runner([]),
    )

    assert result["secrets"] == "removed"
    assert {
        relative_path: context.paths.secret_directory.joinpath(
            *relative_path.split("/")
        ).read_bytes()
        for relative_path in expected_contents
    } == expected_contents
    assert (
        bootstrap_secret_set(
            context.paths,
            random_bytes=lambda size: bytes(range(32, 32 + size)),
            application_secret_references=context.application_secret_references,
        )
        is SecretSetState.COMPLETE
    )
    assert {
        relative_path: context.paths.secret_directory.joinpath(
            *relative_path.split("/")
        ).read_bytes()
        for relative_path in expected_contents
    } == expected_contents


def test_cli_bootstrap_accepts_runtime_configured_secret_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = resolve_stack_paths(tmp_path)
    expected_contents = {
        "authentication/current.key": b"current-application-key",
        "authentication/previous.key": b"previous-application-key",
        "policy/current.pem": b"policy-application-key",
    }
    for relative_path, content in expected_contents.items():
        secret_path = paths.secret_directory.joinpath(*relative_path.split("/"))
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_bytes(content)
        secret_path.chmod(0o600)
    monkeypatch.setenv("KNOWLEDGE_AUTH_CURRENT_KEY_ID", "auth-key-current")
    monkeypatch.setenv("KNOWLEDGE_AUTH_CURRENT_KEY_FILE", "authentication/current.key")
    monkeypatch.setenv(
        "KNOWLEDGE_AUTH_PREVIOUS_KEYS",
        "auth-key-previous=authentication/previous.key",
    )
    monkeypatch.setenv("KNOWLEDGE_POLICY_SIGNING_KEY_FILE", "policy/current.pem")
    monkeypatch.setattr(stack_module, "resolve_stack_paths", lambda _root: paths)

    assert stack_module.main(["bootstrap"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "project": "knowledge-local",
        "result_code": "secret_set_ready",
        "secret_set": "complete",
        "state": "ready",
    }
    assert {
        relative_path: paths.secret_directory.joinpath(*relative_path.split("/")).read_bytes()
        for relative_path in expected_contents
    } == expected_contents


@pytest.mark.parametrize(
    ("current_key_id", "previous_keys"),
    [
        ("UPPERCASE-KEY", "auth-key-previous=authentication/previous.key"),
        ("auth-key-current", "auth-key-current=authentication/previous.key"),
    ],
)
def test_cli_status_rejects_invalid_or_colliding_current_authentication_key_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    current_key_id: str,
    previous_keys: str,
) -> None:
    monkeypatch.setenv("KNOWLEDGE_AUTH_CURRENT_KEY_ID", current_key_id)
    monkeypatch.setenv("KNOWLEDGE_AUTH_CURRENT_KEY_FILE", "authentication/current.key")
    monkeypatch.setenv("KNOWLEDGE_AUTH_PREVIOUS_KEYS", previous_keys)

    def forbidden_runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        del arguments, timeout_seconds, environment
        pytest.fail("invalid runtime references must fail before subprocess execution")

    assert stack_module.main(["status"], runner=forbidden_runner) == 65

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "result_code": "application_secret_configuration_invalid",
        "state": "error",
    }
    assert current_key_id not in captured.out


def test_cli_never_prints_raw_exception(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        stack_module.main(
            ["verify"],
            runner=_semantic_probe_runner(failing_service="redis", raw_output="DO_NOT_LEAK"),
        )
        == 75
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "result_code": "redis_contract_failed",
        "state": "error",
    }
    assert "DO_NOT_LEAK" not in captured.out + captured.err


def test_cli_syntax_errors_are_fixed_json_without_argparse_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert stack_module.main(["reset", "--confirm-project", "knowledge-local"]) == 2

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"result_code": "invalid_cli", "state": "error"}


def test_cli_reset_refuses_non_tty_local_invocation_without_ci_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        stack_module.main(
            [
                "reset",
                "--project-name",
                "knowledge-local",
                "--confirm-project",
                "knowledge-local",
            ],
            is_interactive=False,
        )
        == 2
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "result_code": "interactive_confirmation_required",
        "state": "error",
    }


def test_cli_noninteractive_reset_requires_ci_project_and_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CI", "true")

    assert (
        stack_module.main(
            [
                "reset",
                "--project-name",
                "knowledge-local",
                "--confirm-project",
                "knowledge-local",
                "--non-interactive",
            ],
            is_interactive=False,
        )
        == 2
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "result_code": "noninteractive_reset_forbidden",
        "state": "error",
    }


def test_cli_noninteractive_reset_accepts_explicit_ci_scope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CI", "true")

    assert (
        stack_module.main(
            [
                "reset",
                "--project-name",
                "knowledge-ci-unit",
                "--confirm-project",
                "knowledge-ci-unit",
                "--non-interactive",
            ],
            runner=_reset_runner([], project_volumes=set()),
            is_interactive=False,
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "project": "knowledge-ci-unit",
        "removed_volumes": 0,
        "result_code": "stack_reset_complete",
        "secrets": "preserved",
        "state": "absent",
    }


@pytest.mark.parametrize(
    ("environment", "result_code"),
    [
        ({"POSTGRES_PORT": "not-a-port"}, "invalid_port"),
        ({"POSTGRES_PORT": "15432", "REDIS_PORT": "15432"}, "duplicate_port"),
    ],
)
def test_cli_maps_invalid_port_contract_to_prerequisite_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: dict[str, str],
    result_code: str,
) -> None:
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)

    assert stack_module.main(["config"]) == 64

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"result_code": result_code, "state": "error"}


def test_cli_verify_success_has_fixed_redacted_json_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert stack_module.main(["verify"], runner=_semantic_probe_runner()) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert set(payload) == {"project", "state", "probes", "result_code"}
    assert payload["project"] == "knowledge-local"
    assert payload["state"] == "ready"
    assert payload["result_code"] == "stack_verified"
    assert [probe["service"] for probe in payload["probes"]] == [
        "postgresql",
        "qdrant",
        "neo4j",
        "redis",
        "temporal",
        "temporal-ui",
    ]
    assert all(
        set(probe) == {"service", "is_ready", "result_code", "latency_ms"}
        for probe in payload["probes"]
    )


def test_cli_smoke_requires_explicit_project_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CI", "true")

    assert stack_module.main(["smoke", "--project-name", "knowledge-ci-unit"]) == 2

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "result_code": "invalid_cli",
        "state": "error",
    }


@pytest.fixture
def ci_stack_context(stack_context: Any) -> Any:
    return stack_module.StackContext(
        stack_context.paths,
        "knowledge-ci-unit",
        stack_context.ports,
        {**stack_context.environment, "CI": "true"},
    )


class _RecordingSmokeOperations:
    def __init__(
        self,
        events: list[str],
        *,
        fail_at: str | None = None,
        recovery_fails: bool = False,
    ) -> None:
        self._events = events
        self._fail_at = fail_at
        self._recovery_fails = recovery_fails
        self._up_count = 0
        self._verify_count = 0

    def _record(self, event: str) -> None:
        self._events.append(event)
        if event == self._fail_at:
            raise StackFailure(StackExitCode.READINESS, "fixed_smoke_failure")

    def reset_before(self, context: StackContext) -> None:
        del context
        self._record("reset-before")

    def bootstrap(self, context: StackContext) -> None:
        del context
        self._record("bootstrap")

    def config(self, context: StackContext) -> None:
        del context
        self._record("config")

    def up(self, context: StackContext) -> None:
        del context
        self._up_count += 1
        self._record(("up-first", "up-second", "up-idempotent")[self._up_count - 1])

    def verify(self, context: StackContext) -> None:
        del context
        self._verify_count += 1
        self._record(("verify-first", "verify-second", "verify-idempotent")[self._verify_count - 1])

    def new_markers(self, context: StackContext) -> SmokeMarkerSet:
        del context
        return SmokeMarkerSet(
            marker_key="recording",
            qdrant_collection="recording",
            qdrant_point_id=1,
            redis_key="recording",
        )

    def create_markers(self, context: StackContext, markers: SmokeMarkerSet) -> None:
        del context, markers
        self._record("create-markers")

    def down_preserve(self, context: StackContext) -> None:
        del context
        self._record("down-preserve")

    def verify_markers(self, context: StackContext, markers: SmokeMarkerSet) -> None:
        del context, markers
        self._record("verify-markers")

    def stop_redis(self, context: StackContext) -> None:
        del context
        self._record("stop-redis")

    def verify_outage(self, context: StackContext) -> None:
        del context
        self._record("verify-outage")

    def start_redis(self, context: StackContext) -> None:
        del context
        self._record("start-redis")

    def verify_recovery(self, context: StackContext) -> None:
        del context
        self._record("verify-recovery")

    def ensure_redis_recovered(self, context: StackContext) -> None:
        del context
        self._events.append("ensure-redis-recovered")
        if self._recovery_fails:
            raise StackFailure(StackExitCode.READINESS, "redis_recovery_not_proven")

    def remove_markers(self, context: StackContext, markers: SmokeMarkerSet) -> None:
        del context, markers
        self._record("remove-markers")

    def reset_after(self, context: StackContext) -> None:
        del context
        self._record("reset-after")


def test_smoke_runs_exact_contract_order(ci_stack_context: Any) -> None:
    events: list[str] = []
    run_smoke_contract = getattr(stack_module, "run_smoke_contract", None)
    assert callable(run_smoke_contract), "smoke orchestration is absent"

    run_smoke_contract(ci_stack_context, operations=_RecordingSmokeOperations(events))

    assert events == [
        "reset-before",
        "bootstrap",
        "config",
        "up-first",
        "verify-first",
        "create-markers",
        "down-preserve",
        "up-second",
        "verify-second",
        "verify-markers",
        "up-idempotent",
        "verify-idempotent",
        "stop-redis",
        "verify-outage",
        "start-redis",
        "verify-recovery",
        "remove-markers",
        "reset-after",
    ]


def test_smoke_finally_resets_after_mid_run_failure(ci_stack_context: Any) -> None:
    events: list[str] = []
    run_smoke_contract = getattr(stack_module, "run_smoke_contract", None)
    assert callable(run_smoke_contract), "smoke orchestration is absent"

    with pytest.raises(StackFailure):
        run_smoke_contract(
            ci_stack_context,
            operations=_RecordingSmokeOperations(events, fail_at="verify-markers"),
        )

    assert events[-1] == "reset-after"


@pytest.mark.parametrize(
    "failure_event",
    ["stop-redis", "start-redis", "verify-recovery"],
)
def test_smoke_proves_redis_recovery_before_cleanup_and_preserves_primary_failure(
    ci_stack_context: Any,
    failure_event: str,
) -> None:
    events: list[str] = []

    with pytest.raises(StackFailure) as raised:
        stack_module.run_smoke_contract(
            ci_stack_context,
            operations=_RecordingSmokeOperations(events, fail_at=failure_event),
        )

    assert str(raised.value) == "fixed_smoke_failure"
    assert events[-3:] == [
        "ensure-redis-recovered",
        "remove-markers",
        "reset-after",
    ]


@pytest.mark.parametrize(
    "failure_event",
    ["stop-redis", "start-redis", "verify-recovery"],
)
def test_smoke_fails_closed_without_reset_when_redis_recovery_cannot_be_proven(
    ci_stack_context: Any,
    failure_event: str,
) -> None:
    events: list[str] = []

    with pytest.raises(StackFailure) as raised:
        stack_module.run_smoke_contract(
            ci_stack_context,
            operations=_RecordingSmokeOperations(
                events,
                fail_at=failure_event,
                recovery_fails=True,
            ),
        )

    assert str(raised.value) == "fixed_smoke_failure"
    assert events[-1] == "ensure-redis-recovered"
    assert "remove-markers" not in events
    assert "reset-after" not in events


def test_smoke_removes_partial_marker_set_before_final_reset(
    ci_stack_context: Any,
) -> None:
    events: list[str] = []

    with pytest.raises(StackFailure) as raised:
        stack_module.run_smoke_contract(
            ci_stack_context,
            operations=_RecordingSmokeOperations(events, fail_at="create-markers"),
        )

    assert str(raised.value) == "fixed_smoke_failure"
    assert events[-3:] == ["create-markers", "remove-markers", "reset-after"]


def test_smoke_is_ci_only(stack_context: Any) -> None:
    events: list[str] = []
    run_smoke_contract = getattr(stack_module, "run_smoke_contract", None)
    assert callable(run_smoke_contract), "smoke orchestration is absent"

    with pytest.raises(StackFailure) as raised:
        run_smoke_contract(stack_context, operations=_RecordingSmokeOperations(events))

    assert raised.value.exit_code is StackExitCode.CLI
    assert events == []


def _smoke_marker_runner(
    calls: list[tuple[tuple[str, ...], float]],
    *,
    stage: str,
    failing_service: str | None = None,
) -> Any:
    def run(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        del environment
        command = tuple(arguments)
        calls.append((command, timeout_seconds))
        service = command[command.index("--no-TTY") + 1]
        if service == failing_service:
            return stack_module.CommandResult(1, "DO_NOT_LEAK", "DO_NOT_LEAK")
        return stack_module.CommandResult(0, f"smoke_markers_{stage}\n", "")

    return run


def test_create_smoke_markers_uses_authenticated_in_container_clients_with_deadlines(
    ci_stack_context: Any,
) -> None:
    calls: list[tuple[tuple[str, ...], float]] = []
    create_markers = getattr(stack_module, "_create_smoke_markers", None)
    assert callable(create_markers), "authenticated marker creation is absent"

    markers = create_markers(
        ci_stack_context,
        runner=_smoke_marker_runner(calls, stage="created"),
        token_bytes=lambda count: bytes(range(count)),
    )

    assert markers == stack_module.SmokeMarkerSet(
        marker_key="000102030405060708090a0b",
        qdrant_collection="stack_smoke_marker_000102030405060708090a0b",
        qdrant_point_id=1,
        redis_key="stack:smoke:000102030405060708090a0b",
    )
    assert {command[command.index("--no-TTY") + 1] for command, _ in calls} == {
        "postgresql",
        "qdrant",
        "neo4j",
        "redis",
    }
    assert all(0 < timeout_seconds <= 10 for _, timeout_seconds in calls)
    scripts = {command[command.index("--no-TTY") + 1]: command[-1] for command, _ in calls}
    assert "postgres_application_password" in scripts["postgresql"]
    assert "qdrant_config.yaml" in scripts["qdrant"]
    assert "neo4j_auth" in scripts["neo4j"]
    assert "redis_acl" in scripts["redis"]
    assert all("DO_NOT_LEAK" not in part for command, _ in calls for part in command)


@pytest.mark.parametrize("failing_service", ["qdrant", "neo4j", "redis"])
def test_partial_marker_creation_rolls_back_all_exact_marker_types(
    ci_stack_context: Any,
    failing_service: str,
) -> None:
    calls: list[tuple[str, str]] = []

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        del timeout_seconds, environment
        command = tuple(arguments)
        service = command[command.index("--no-TTY") + 1]
        script = command[-1]
        stage = "remove" if "smoke_markers_removed" in script else "create"
        calls.append((stage, service))
        if stage == "create" and service == failing_service:
            return stack_module.CommandResult(1, "DO_NOT_LEAK", "DO_NOT_LEAK")
        return stack_module.CommandResult(0, f"smoke_markers_{stage}d\n", "")

    with pytest.raises(StackFailure) as raised:
        stack_module._create_smoke_markers(
            ci_stack_context,
            runner=runner,
            token_bytes=lambda count: bytes(range(count)),
        )

    assert str(raised.value) == "smoke_marker_create_failed"
    assert calls[-4:] == [
        ("remove", "postgresql"),
        ("remove", "qdrant"),
        ("remove", "neo4j"),
        ("remove", "redis"),
    ]


def test_verify_smoke_markers_maps_dependency_output_to_fixed_failure(
    ci_stack_context: Any,
) -> None:
    calls: list[tuple[tuple[str, ...], float]] = []
    verify_markers = getattr(stack_module, "_verify_smoke_markers", None)
    assert callable(verify_markers), "authenticated marker verification is absent"
    markers = stack_module.SmokeMarkerSet(
        marker_key="a" * 24,
        qdrant_collection=f"stack_smoke_marker_{'a' * 24}",
        qdrant_point_id=1,
        redis_key=f"stack:smoke:{'a' * 24}",
    )

    with pytest.raises(StackFailure) as raised:
        verify_markers(
            ci_stack_context,
            markers,
            runner=_smoke_marker_runner(calls, stage="verified", failing_service="qdrant"),
        )

    assert raised.value.exit_code is StackExitCode.READINESS
    assert str(raised.value) == "smoke_marker_verify_failed"
    assert "DO_NOT_LEAK" not in str(raised.value)


def test_remove_smoke_markers_deletes_all_four_exact_marker_types(
    ci_stack_context: Any,
) -> None:
    calls: list[tuple[tuple[str, ...], float]] = []
    remove_markers = getattr(stack_module, "_remove_smoke_markers", None)
    assert callable(remove_markers), "authenticated marker cleanup is absent"
    markers = stack_module.SmokeMarkerSet(
        marker_key="b" * 24,
        qdrant_collection=f"stack_smoke_marker_{'b' * 24}",
        qdrant_point_id=1,
        redis_key=f"stack:smoke:{'b' * 24}",
    )

    remove_markers(
        ci_stack_context,
        markers,
        runner=_smoke_marker_runner(calls, stage="removed"),
    )

    assert [command[command.index("--no-TTY") + 1] for command, _ in calls] == [
        "postgresql",
        "qdrant",
        "neo4j",
        "redis",
    ]


def test_verify_reports_stopped_redis_as_redis_contract_failure(stack_context: Any) -> None:
    stopped_rows = json.loads(_healthy_ps_output())
    for row in stopped_rows:
        if row["Service"] == "redis":
            row.update({"State": "exited", "Health": "", "ExitCode": 0})
    base_runner = _semantic_probe_runner(failing_service="redis")

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        command = tuple(arguments)
        if "ps" in command:
            return stack_module.CommandResult(0, json.dumps(stopped_rows), "")
        return base_runner(
            arguments,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )

    with pytest.raises(StackFailure) as raised:
        stack_module.verify_stack(stack_context, runner=runner)

    assert raised.value.exit_code is StackExitCode.READINESS
    assert str(raised.value) == "redis_contract_failed"


def test_cli_smoke_runs_contract_with_exact_ci_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    observed_projects: list[str] = []

    def smoke(context: Any, *, runner: Any) -> None:
        del runner
        observed_projects.append(context.project_name)

    monkeypatch.setattr(stack_module, "run_smoke_contract", smoke)
    monkeypatch.setenv("CI", "true")

    assert (
        stack_module.main(
            [
                "smoke",
                "--project-name",
                "knowledge-ci-unit",
                "--confirm-project",
                "knowledge-ci-unit",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "project": "knowledge-ci-unit",
        "result_code": "stack_smoke_complete",
        "state": "absent",
    }
    assert observed_projects == ["knowledge-ci-unit"]


def test_smoke_redis_stop_and_start_are_exact_bounded_compose_operations(
    ci_stack_context: Any,
) -> None:
    calls: list[tuple[tuple[str, ...], float]] = []
    stop_redis = getattr(stack_module, "_stop_smoke_redis", None)
    start_redis = getattr(stack_module, "_start_smoke_redis", None)
    assert callable(stop_redis), "bounded Redis outage operation is absent"
    assert callable(start_redis), "bounded Redis recovery operation is absent"

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        del environment
        command = tuple(arguments)
        calls.append((command, timeout_seconds))
        if "ps" in command:
            return stack_module.CommandResult(0, _healthy_ps_output(), "")
        return stack_module.CommandResult(0, "", "")

    stop_redis(ci_stack_context, runner=runner)
    start_redis(ci_stack_context, runner=runner)

    prefix = tuple(stack_module.compose_arguments(ci_stack_context))
    assert ((*prefix, "stop", "--timeout", "15", "redis"), 30.0) in calls
    assert ((*prefix, "start", "redis"), 30.0) in calls
    assert all("reset" not in command and "down" not in command for command, _ in calls)
    assert all(0 < timeout_seconds <= 30 for _, timeout_seconds in calls)


def test_default_redis_recovery_still_verifies_when_start_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
    ci_stack_context: Any,
) -> None:
    events: list[str] = []

    def fail_start(context: Any, *, runner: Any) -> None:
        del context, runner
        events.append("start-failed")
        raise StackFailure(StackExitCode.STARTUP, "smoke_redis_start_failed")

    def verify(context: Any, *, runner: Any) -> tuple[Any, ...]:
        del context, runner
        events.append("verify-passed")
        return ()

    monkeypatch.setattr(stack_module, "_start_smoke_redis", fail_start)
    monkeypatch.setattr(stack_module, "verify_stack", verify)
    operations = stack_module._DefaultSmokeOperations(_successful_lifecycle_runner())

    operations.ensure_redis_recovered(ci_stack_context)

    assert events == ["start-failed", "verify-passed"]


def test_default_redis_recovery_fails_when_full_verify_cannot_prove_health(
    monkeypatch: pytest.MonkeyPatch,
    ci_stack_context: Any,
) -> None:
    monkeypatch.setattr(stack_module, "_start_smoke_redis", lambda *args, **kwargs: None)

    def fail_verify(context: Any, *, runner: Any) -> tuple[Any, ...]:
        del context, runner
        raise StackFailure(StackExitCode.READINESS, "redis_contract_failed")

    monkeypatch.setattr(stack_module, "verify_stack", fail_verify)
    operations = stack_module._DefaultSmokeOperations(_successful_lifecycle_runner())

    with pytest.raises(StackFailure) as raised:
        operations.ensure_redis_recovered(ci_stack_context)

    assert raised.value.exit_code is StackExitCode.READINESS
    assert str(raised.value) == "redis_contract_failed"


def test_smoke_outage_requires_fixed_redis_readiness_failure(ci_stack_context: Any) -> None:
    verify_outage = getattr(stack_module, "_verify_smoke_redis_outage", None)
    assert callable(verify_outage), "Redis outage assertion is absent"
    stopped_rows = json.loads(_healthy_ps_output())
    for row in stopped_rows:
        if row["Service"] == "redis":
            row.update({"State": "exited", "Health": "", "ExitCode": 0})
    base_runner = _semantic_probe_runner(failing_service="redis")

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        command = tuple(arguments)
        if "ps" in command:
            return stack_module.CommandResult(0, json.dumps(stopped_rows), "")
        return base_runner(
            arguments,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )

    verify_outage(ci_stack_context, runner=runner)


def test_smoke_cleanup_inventory_uses_only_exact_project_label(
    ci_stack_context: Any,
) -> None:
    calls: list[tuple[str, ...]] = []
    assert_cleanup = getattr(stack_module, "_assert_smoke_project_absent", None)
    assert callable(assert_cleanup), "exact final Docker inventory assertion is absent"

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        del timeout_seconds, environment
        calls.append(tuple(arguments))
        return stack_module.CommandResult(0, "", "")

    assert_cleanup(ci_stack_context, runner=runner)

    expected_filter = "label=com.docker.compose.project=knowledge-ci-unit"
    assert calls == [
        ("docker", "container", "ls", "--all", "--quiet", "--filter", expected_filter),
        ("docker", "network", "ls", "--quiet", "--filter", expected_filter),
        ("docker", "volume", "ls", "--quiet", "--filter", expected_filter),
    ]


def test_default_smoke_cleanup_rejects_changed_complete_secret_set(
    monkeypatch: pytest.MonkeyPatch, ci_stack_context: Any
) -> None:
    default_operations = getattr(stack_module, "_DefaultSmokeOperations", None)
    assert default_operations is not None, "default smoke operations are absent"
    monkeypatch.setattr(stack_module, "reset_stack", lambda *args, **kwargs: {})

    def runner(arguments: Any, *, timeout_seconds: float, environment: Any = None) -> Any:
        del arguments, timeout_seconds, environment
        return stack_module.CommandResult(0, "", "")

    operations = default_operations(runner)
    operations.bootstrap(ci_stack_context)
    secret_path = ci_stack_context.paths.secret_directory / "redis_application_password"
    original = secret_path.read_text(encoding="ascii")
    secret_path.write_text(f"{original}x", encoding="ascii")

    with pytest.raises(StackFailure) as raised:
        operations.reset_after(ci_stack_context)

    assert raised.value.exit_code is StackExitCode.CONTRACT
    assert str(raised.value) == "smoke_secret_set_changed"


def test_smoke_idempotent_up_reuses_ports_owned_by_verified_project(
    monkeypatch: pytest.MonkeyPatch, ci_stack_context: Any
) -> None:
    repeat_up = getattr(stack_module, "_repeat_smoke_stack_up", None)
    assert callable(repeat_up), "already-running smoke startup is absent"
    calls: list[tuple[tuple[str, ...], float, dict[str, str]]] = []

    def reject_port_recheck(ports: Any) -> None:
        del ports
        pytest.fail("an already-running verified project owns its published ports")

    monkeypatch.setattr(stack_module, "validate_port_availability", reject_port_recheck)

    repeat_up(ci_stack_context, runner=_successful_lifecycle_runner(calls))

    compose_prefix = tuple(stack_module.compose_arguments(ci_stack_context))
    assert any(
        command[: len(compose_prefix)] == compose_prefix and "up" in command
        for command, _, _ in calls
    )
    assert any(_semantic_probe_service(command) is not None for command, _, _ in calls)


@pytest.mark.parametrize("exit_code", [0, 2, 64, 65, 69, 70, 75])
def test_cli_exit_code_set_is_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exit_code: int,
) -> None:
    if exit_code == 0:
        runner = _semantic_probe_runner()
        arguments = ["verify"]
    elif exit_code == 2:
        runner = _semantic_probe_runner()
        arguments = ["unknown-command"]
    else:
        stack_exit_code = StackExitCode(exit_code)

        def fail_verify(context: Any, *, runner: Any, **kwargs: Any) -> Any:
            raise StackFailure(stack_exit_code, f"fixed_{exit_code}")

        monkeypatch.setattr(stack_module, "verify_stack", fail_verify)
        runner = _semantic_probe_runner()
        arguments = ["verify"]

    assert stack_module.main(arguments, runner=runner) == exit_code
    captured = capsys.readouterr()
    assert captured.err == ""
    assert isinstance(json.loads(captured.out), dict)


def test_cli_unexpected_exception_is_redacted_internal_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_verify(context: Any, *, runner: Any, **kwargs: Any) -> Any:
        raise RuntimeError("DO_NOT_LEAK")

    monkeypatch.setattr(stack_module, "verify_stack", fail_verify)

    assert stack_module.main(["verify"], runner=_semantic_probe_runner()) == 70
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "result_code": "lifecycle_internal_error",
        "state": "error",
    }
    assert "DO_NOT_LEAK" not in captured.out
