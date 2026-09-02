"""Bootstrap disposable Web authentication before the live Obsidian journey.

The process composes repository-owned operator boundaries and emits one closed
status document. Child output and HTTP provisioning material never cross this
process boundary.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Final, Protocol, TextIO, cast
from uuid import UUID

import httpx

from personal_os.authentication.totp import TOTP_PERIOD_SECONDS, time_step_of, totp_code
from personal_os.runtime_configuration.secret_files import read_secret_file

_PROJECT_NAME_PATTERN: Final = re.compile(r"^knowledge-ci-[a-z0-9][a-z0-9-]{0,40}$")
_TOTP_CODE_PATTERN: Final = re.compile(r"^[0-9]{6}$")
_LITERAL_EXPORT_PATTERN: Final = re.compile(r"^export ([A-Z][A-Z0-9_]*)=(.*)$")
_REQUIRED_LAUNCHER_NAMES: Final = frozenset(
    {
        "KNOWLEDGE_DATABASE_HOST",
        "KNOWLEDGE_DATABASE_PORT",
        "KNOWLEDGE_DATABASE_NAME",
        "KNOWLEDGE_DATABASE_USER",
        "KNOWLEDGE_DATABASE_PASSWORD_FILE",
        "KNOWLEDGE_DATABASE_SSL_MODE",
        "KNOWLEDGE_AUTH_ALLOWED_ORIGIN",
        "KNOWLEDGE_AUTH_CURRENT_KEY_ID",
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE",
        "KNOWLEDGE_AUTH_MIN_PLUGIN_VERSION",
        "KNOWLEDGE_AUTH_MAX_PLUGIN_VERSION",
        "KNOWLEDGE_POLICY_SIGNING_KEY_ID",
        "KNOWLEDGE_POLICY_SIGNING_KEY_FILE",
    }
)
_COMMAND_TIMEOUT_SECONDS: Final = 180.0
_POLICY_TIMEOUT_SECONDS: Final = 240.0
_WDIO_TIMEOUT_SECONDS: Final = 900.0
_WDIO_PHASE_STATUS_MAX_BYTES: Final = 128
_WDIO_PHASE_FAILURE_CODES: Final[Mapping[str, str]] = {
    "source_lifecycle_scenario_started": "obsidian_wdio_failed_during_onboarding",
    "source_lifecycle_onboarding_completed": "obsidian_wdio_failed_after_onboarding",
    "source_lifecycle_initial_sync_completed": "obsidian_wdio_failed_after_initial_sync",
    "source_lifecycle_rename_completed": "obsidian_wdio_failed_after_rename",
    "source_lifecycle_move_completed": "obsidian_wdio_failed_after_move",
    "source_lifecycle_delete_completed": "obsidian_wdio_failed_after_delete",
    "source_lifecycle_restore_completed": "obsidian_wdio_failed_after_restore",
    "source_lifecycle_journal_drained": "obsidian_wdio_failed_after_journal_drain",
    "source_lifecycle_journey_completed": "obsidian_wdio_failed_after_journey",
    "automatic_existing_note_committed": (
        "obsidian_wdio_failed_after_automatic_existing_note_commit"
    ),
    "automatic_new_note_committed": "obsidian_wdio_failed_after_automatic_new_note_commit",
    "automatic_policy_successor_committed": (
        "obsidian_wdio_failed_after_automatic_policy_successor_commit"
    ),
    "automatic_convergence_journey_completed": (
        "obsidian_wdio_failed_after_automatic_convergence_journey"
    ),
    "device_sync_scenario_started": "obsidian_wdio_failed_during_device_sync_onboarding",
    "device_sync_onboarding_completed": "obsidian_wdio_failed_after_device_sync_onboarding",
    "device_sync_remote_edit_no_echo_completed": (
        "obsidian_wdio_failed_after_device_sync_remote_edit_no_echo"
    ),
    "device_sync_cursor_gap_repair_completed": (
        "obsidian_wdio_failed_after_device_sync_cursor_gap_repair"
    ),
    "device_sync_lost_sqlite_repair_completed": (
        "obsidian_wdio_failed_after_device_sync_lost_sqlite_repair"
    ),
    "device_sync_remote_tombstone_completed": (
        "obsidian_wdio_failed_after_device_sync_remote_tombstone"
    ),
    "device_sync_journey_completed": "obsidian_wdio_failed_after_device_sync_journey",
    "multipart_journey_started": "obsidian_wdio_failed_after_multipart_journey_start",
    "multipart_resume_committed": "obsidian_wdio_failed_after_multipart_resume",
    "multipart_corruption_refused": "obsidian_wdio_failed_after_multipart_corruption_refusal",
    "multipart_lost_ack_replayed": "obsidian_wdio_failed_after_multipart_lost_ack_replay",
    "multipart_policy_denial_observed": "obsidian_wdio_failed_after_multipart_policy_denial",
    "multipart_journey_completed": "obsidian_wdio_failed_after_multipart_journey",
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded child outcome; content remains private to the orchestrator."""

    return_code: int
    stdout: str
    stderr: str


class CommandExecutor(Protocol):
    """External process boundary used by the live orchestrator."""

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str],
        cwd: Path,
        timeout_seconds: float,
        should_capture: bool,
    ) -> CommandResult: ...


class SubprocessCommandExecutor:
    """Run one child while suppressing every child-owned diagnostic stream."""

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str],
        cwd: Path,
        timeout_seconds: float,
        should_capture: bool,
    ) -> CommandResult:
        stdout_target: int = subprocess.PIPE if should_capture else subprocess.DEVNULL
        stderr_target: int = subprocess.PIPE if should_capture else subprocess.DEVNULL
        try:
            completed = subprocess.run(
                list(arguments),
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=stdout_target,
                stderr=stderr_target,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except OSError, subprocess.TimeoutExpired:
            return CommandResult(70, "", "")
        return CommandResult(
            completed.returncode,
            completed.stdout if isinstance(completed.stdout, str) else "",
            completed.stderr if isinstance(completed.stderr, str) else "",
        )


@dataclass(frozen=True, slots=True)
class LiveAcceptanceConfig:
    """Validated non-secret paths, identities, origins, and child environment."""

    repository_root: Path
    project_name: str
    username: str
    workspace_key: str
    server_origin: str
    allowed_origin: str
    password_file: Path
    runtime_environment: Mapping[str, str]
    totp_helper: str = ".local/e2e-totp-code.py"
    policy_helper: str = ".local/publish-policy-revision.py"
    wdio_spec: str = "test/specs/source-lifecycle.e2e.ts"
    policy_key_file_name: str = "policy_signing_b.pem"
    keep_wdio_phase_status: bool = False


_EXCEPTION_BOOKKEEPING_FIELDS: Final[frozenset[str]] = frozenset(
    {"__traceback__", "__cause__", "__context__", "__suppress_context__", "__notes__"}
)


class LiveAcceptanceFailure(Exception):
    """Closed failure whose result code contains no provider-owned text.

    Hand-rolled immutability instead of a frozen dataclass: Python 3.14's
    context machinery assigns exception bookkeeping fields while a failure
    propagates through ``with`` blocks, and a frozen dataclass turns that
    into ``FrozenInstanceError`` instead of raising the failure itself.
    ``__setattr__`` allows exactly those bookkeeping fields.
    """

    result_code: str

    def __init__(self, result_code: str) -> None:
        object.__setattr__(self, "result_code", result_code)

    def __setattr__(self, name: str, value: object) -> None:
        if name in _EXCEPTION_BOOKKEEPING_FIELDS:
            object.__setattr__(self, name, value)
            return
        raise FrozenInstanceError(f"cannot assign to field {name!r}")

    def __repr__(self) -> str:
        return f"LiveAcceptanceFailure(result_code={self.result_code!r})"


HttpClientFactory = Callable[[], httpx.Client]


def _run_child(
    executor: CommandExecutor,
    config: LiveAcceptanceConfig,
    arguments: Sequence[str],
    *,
    failure_code: str,
    timeout_seconds: float = _COMMAND_TIMEOUT_SECONDS,
    should_capture: bool = False,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    result = executor.run(
        arguments,
        environment=config.runtime_environment if environment is None else environment,
        cwd=config.repository_root,
        timeout_seconds=timeout_seconds,
        should_capture=should_capture,
    )
    if result.return_code != 0:
        raise LiveAcceptanceFailure(failure_code)
    return result


def _parse_json_document(raw_document: str, failure_code: str) -> Mapping[str, object]:
    try:
        parsed: object = json.loads(raw_document)
    except json.JSONDecodeError, TypeError:
        raise LiveAcceptanceFailure(failure_code) from None
    if not isinstance(parsed, dict):
        raise LiveAcceptanceFailure(failure_code)
    return cast(Mapping[str, object], parsed)


def _cookie_headers(response: httpx.Response) -> tuple[str, str]:
    pairs = tuple(value.split(";", 1)[0] for value in response.headers.get_list("set-cookie"))
    csrf_pair = next((pair for pair in pairs if "csrf" in pair.lower()), None)
    if csrf_pair is None or "=" not in csrf_pair or not pairs:
        raise LiveAcceptanceFailure("totp_login_cookie_invalid")
    return "; ".join(pairs), csrf_pair.split("=", 1)[1]


def _require_http_success(response: httpx.Response, failure_code: str) -> Mapping[str, object]:
    if response.status_code != 200:
        raise LiveAcceptanceFailure(failure_code)
    return _parse_json_document(response.text, failure_code)


def _activate_totp_over_http(
    config: LiveAcceptanceConfig, client_factory: HttpClientFactory
) -> int:
    secret_root_value = config.runtime_environment.get("KNOWLEDGE_SECRET_ROOT")
    secret_root = (
        Path(secret_root_value) if secret_root_value is not None else config.password_file.parent
    )
    password = read_secret_file(config.password_file, secret_root).get_secret_value()
    try:
        with client_factory() as client:
            login = client.post(
                "/api/auth/login",
                headers={"origin": config.allowed_origin},
                json={"username": config.username, "password": password},
            )
            _require_http_success(login, "totp_login_failed")
            cookie, csrf = _cookie_headers(login)
            protected_headers = {
                "origin": config.allowed_origin,
                "cookie": cookie,
                "x-csrf-token": csrf,
            }
            started = client.post(
                "/api/auth/totp/enrollments",
                headers=protected_headers,
                json={"action": "start"},
            )
            enrollment_document = _require_http_success(started, "totp_enrollment_start_failed")
            data = enrollment_document.get("data")
            if not isinstance(data, dict):
                raise LiveAcceptanceFailure("totp_enrollment_response_invalid")
            enrollment = data.get("enrollment")
            if not isinstance(enrollment, dict):
                raise LiveAcceptanceFailure("totp_enrollment_response_invalid")
            enrollment_id_value = enrollment.get("enrollment_id")
            secret_value = enrollment.get("secret")
            if not isinstance(enrollment_id_value, str) or not isinstance(secret_value, str):
                raise LiveAcceptanceFailure("totp_enrollment_response_invalid")
            try:
                enrollment_id = UUID(enrollment_id_value)
                secret = base64.b32decode(secret_value, casefold=False)
            except ValueError, TypeError:
                raise LiveAcceptanceFailure("totp_enrollment_response_invalid") from None
            activation_unix_time_seconds = int(time.time())
            code = totp_code(secret=secret, unix_time_seconds=activation_unix_time_seconds)
            verified = client.post(
                f"/api/auth/totp/enrollments/{enrollment_id}/verify",
                headers=protected_headers,
                json={"code": code},
            )
            _require_http_success(verified, "totp_enrollment_verify_failed")
            return time_step_of(unix_time_seconds=activation_unix_time_seconds)
    except LiveAcceptanceFailure:
        raise
    except OSError, httpx.HTTPError:
        raise LiveAcceptanceFailure("totp_http_unavailable") from None


def _wait_for_unused_totp_step(activation_time_step: int) -> None:
    while True:
        unix_time_seconds = time.time()
        if time_step_of(unix_time_seconds=int(unix_time_seconds)) > activation_time_step:
            return
        next_step_unix_time_seconds = (activation_time_step + 1) * TOTP_PERIOD_SECONDS
        time.sleep(max(float(next_step_unix_time_seconds) - unix_time_seconds, 0.01))


def _totp_preflight(config: LiveAcceptanceConfig, executor: CommandExecutor) -> bool:
    result = executor.run(
        ["uv", "run", "python", config.totp_helper],
        environment=config.runtime_environment,
        cwd=config.repository_root,
        timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
        should_capture=True,
    )
    return (
        result.return_code == 0 and _TOTP_CODE_PATTERN.fullmatch(result.stdout.strip()) is not None
    )


def _live_child_environment(config: LiveAcceptanceConfig) -> Mapping[str, str]:
    environment = dict(config.runtime_environment)
    environment.update(
        {
            "E2E_SERVER_ORIGIN": config.server_origin,
            # One public origin serves the browser and the plugin fixtures:
            # the plugin origin is the validated allowed origin itself, and
            # a local E2E_SERVER_ORIGIN is never a plugin setting.
            "E2E_ALLOWED_ORIGIN": config.allowed_origin,
            "E2E_PLUGIN_ORIGIN": config.allowed_origin,
            "E2E_WEB_USERNAME": config.username,
            "E2E_WEB_PASSWORD_FILE": str(config.password_file),
            "E2E_TOTP_HELPER": config.totp_helper,
        }
    )
    return environment


def _wdio_phase_status_path(config: LiveAcceptanceConfig) -> Path:
    return config.repository_root / ".local" / (f"{config.project_name}.obsidian-live-phase.json")


def _remove_wdio_phase_status(status_path: Path) -> None:
    try:
        status_path.unlink(missing_ok=True)
        status_path.with_name(f"{status_path.name}.diagnostic.json").unlink(missing_ok=True)
    except OSError:
        raise LiveAcceptanceFailure("obsidian_wdio_failed") from None


def _closed_wdio_failure_code(status_path: Path) -> str:
    try:
        if status_path.stat().st_size > _WDIO_PHASE_STATUS_MAX_BYTES:
            return "obsidian_wdio_failed"
        document = _parse_json_document(
            status_path.read_text(encoding="utf-8"),
            "obsidian_wdio_failed",
        )
    except LiveAcceptanceFailure, OSError, UnicodeError:
        return "obsidian_wdio_failed"
    if set(document) != {"result_code"}:
        return "obsidian_wdio_failed"
    result_code = document.get("result_code")
    if not isinstance(result_code, str):
        return "obsidian_wdio_failed"
    return _WDIO_PHASE_FAILURE_CODES.get(result_code, "obsidian_wdio_failed")


def _execute_live_acceptance(
    config: LiveAcceptanceConfig,
    executor: CommandExecutor,
    client_factory: HttpClientFactory,
) -> None:
    if (
        config.runtime_environment.get("CI") != "true"
        or _PROJECT_NAME_PATTERN.fullmatch(config.project_name) is None
    ):
        raise LiveAcceptanceFailure("disposable_project_required")

    stack_status = _run_child(
        executor,
        config,
        [
            "uv",
            "run",
            "python",
            "tools/local_service_stack.py",
            "status",
            "--project-name",
            config.project_name,
        ],
        failure_code="disposable_stack_unavailable",
        should_capture=True,
    )
    if (
        _parse_json_document(stack_status.stdout, "disposable_stack_status_invalid").get("state")
        != "ready"
    ):
        raise LiveAcceptanceFailure("disposable_stack_not_ready")

    _run_child(
        executor,
        config,
        ["uv", "run", "alembic", "upgrade", "head"],
        failure_code="database_migration_failed",
    )
    identity = _run_child(
        executor,
        config,
        [
            "uv",
            "run",
            "python",
            "tools/canonical_core_operations.py",
            "bootstrap-identity",
            "--username",
            config.username,
            "--user-display-name",
            "Disposable Live Operator",
            "--workspace-key",
            config.workspace_key,
            "--workspace-display-name",
            "Disposable Live Workspace",
            "--device-name",
            "Obsidian Live Acceptance",
            "--device-kind",
            "web",
        ],
        failure_code="identity_bootstrap_failed",
        should_capture=True,
    )
    identity_document = _parse_json_document(identity.stdout, "identity_bootstrap_response_invalid")
    workspace_id_value = identity_document.get("workspace_id")
    if not isinstance(workspace_id_value, str):
        raise LiveAcceptanceFailure("identity_bootstrap_response_invalid")
    try:
        workspace_id = UUID(workspace_id_value)
    except ValueError:
        raise LiveAcceptanceFailure("identity_bootstrap_response_invalid") from None

    credential_status = _run_child(
        executor,
        config,
        [
            "uv",
            "run",
            "--package",
            "api-runtime",
            "personal-api",
            "web-credential-status",
            "--username",
            config.username,
        ],
        failure_code="web_credential_status_failed",
        should_capture=True,
    )
    if credential_status.stdout.startswith("enrolled=false "):
        _run_child(
            executor,
            config,
            [
                "uv",
                "run",
                "--package",
                "api-runtime",
                "personal-api",
                "enroll-web-credential",
                "--username",
                config.username,
                "--password-file-name",
                config.password_file.name,
            ],
            failure_code="web_credential_enrollment_failed",
        )
    elif not credential_status.stdout.startswith("enrolled=true "):
        raise LiveAcceptanceFailure("web_credential_status_invalid")

    _run_child(
        executor,
        config,
        [
            "uv",
            "run",
            "--package",
            "api-runtime",
            "personal-api",
            "policy-key",
            "initialize",
            "--workspace-id",
            str(workspace_id),
            "--key-file-name",
            config.policy_key_file_name,
        ],
        failure_code="policy_key_initialization_failed",
    )

    has_active_totp = _totp_preflight(config, executor)
    if not has_active_totp:
        activation_time_step = _activate_totp_over_http(config, client_factory)
        if not _totp_preflight(config, executor):
            raise LiveAcceptanceFailure("active_totp_preflight_failed")
        _wait_for_unused_totp_step(activation_time_step)

    _run_child(
        executor,
        config,
        ["uv", "run", "python", config.policy_helper],
        failure_code="policy_publication_failed",
        timeout_seconds=_POLICY_TIMEOUT_SECONDS,
        environment=_live_child_environment(config),
    )
    policy_time_step = time_step_of(unix_time_seconds=int(time.time()))
    _wait_for_unused_totp_step(policy_time_step)
    wdio_phase_status = _wdio_phase_status_path(config)
    _remove_wdio_phase_status(wdio_phase_status)
    wdio_environment = dict(_live_child_environment(config))
    wdio_environment["E2E_LIVE_PHASE_STATUS_FILE"] = str(wdio_phase_status)
    wdio_result = executor.run(
        [
            "pnpm",
            "--filter",
            "@workspace/obsidian-plugin",
            "exec",
            "wdio",
            "run",
            "wdio.conf.mts",
            "--spec",
            config.wdio_spec,
        ],
        environment=wdio_environment,
        cwd=config.repository_root,
        timeout_seconds=_WDIO_TIMEOUT_SECONDS,
        should_capture=False,
    )
    if wdio_result.return_code != 0:
        failure_code = _closed_wdio_failure_code(wdio_phase_status)
        if not config.keep_wdio_phase_status:
            _remove_wdio_phase_status(wdio_phase_status)
        raise LiveAcceptanceFailure(failure_code)
    if not config.keep_wdio_phase_status:
        _remove_wdio_phase_status(wdio_phase_status)


def _print_status(output: TextIO, state: str, result_code: str) -> None:
    output.write(json.dumps({"result_code": result_code, "state": state}, sort_keys=True))
    output.write("\n")


def _write_final_result(config: LiveAcceptanceConfig, state: str, result_code: str) -> None:
    """Atomically retain the closed outcome after every guarded run."""
    phase_path = _wdio_phase_status_path(config)
    phase_code: str | None = None
    try:
        document = _parse_json_document(
            phase_path.read_text(encoding="utf-8"),
            "obsidian_wdio_failed",
        )
        candidate = document.get("result_code")
        if set(document) == {"result_code"} and isinstance(candidate, str):
            phase_code = candidate
    except LiveAcceptanceFailure, OSError, UnicodeError:
        pass
    result_path = (
        config.repository_root / ".local" / (f"{config.project_name}.obsidian-live-result.json")
    )
    result_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = result_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(
            {"result_code": result_code, "state": state, "wdio_phase": phase_code},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.replace(temporary_path, result_path)


def run_live_acceptance(
    config: LiveAcceptanceConfig,
    *,
    executor: CommandExecutor,
    client_factory: HttpClientFactory,
    output: TextIO,
) -> int:
    """Run the guarded bootstrap and emit exactly one closed status document."""
    try:
        _execute_live_acceptance(config, executor, client_factory)
    except LiveAcceptanceFailure as failure:
        _write_final_result(config, "error", failure.result_code)
        _print_status(output, "error", failure.result_code)
        return 1
    except Exception:
        _write_final_result(config, "error", "live_acceptance_internal_error")
        _print_status(output, "error", "live_acceptance_internal_error")
        return 1
    _write_final_result(config, "complete", "obsidian_live_acceptance_passed")
    _print_status(output, "complete", "obsidian_live_acceptance_passed")
    return 0


def _literal_launcher_exports(repository_root: Path) -> Mapping[str, str]:
    launcher_path = repository_root / ".local" / "serve-local.sh"
    try:
        lines = launcher_path.read_text(encoding="utf-8").splitlines()
    except OSError, UnicodeError:
        raise LiveAcceptanceFailure("local_launcher_unavailable") from None
    exports: dict[str, str] = {}
    for line in lines:
        match = _LITERAL_EXPORT_PATTERN.fullmatch(line.strip())
        if match is None or match.group(1) not in _REQUIRED_LAUNCHER_NAMES:
            continue
        raw_value = match.group(2)
        if "$" in raw_value or "`" in raw_value:
            raise LiveAcceptanceFailure("local_launcher_contract_invalid")
        try:
            tokens = shlex.split(raw_value, posix=True)
        except ValueError:
            raise LiveAcceptanceFailure("local_launcher_contract_invalid") from None
        if len(tokens) != 1:
            raise LiveAcceptanceFailure("local_launcher_contract_invalid")
        exports[match.group(1)] = tokens[0]
    if not _REQUIRED_LAUNCHER_NAMES.issubset(exports):
        raise LiveAcceptanceFailure("local_launcher_contract_invalid")
    return exports


def build_live_acceptance_config(
    *,
    repository_root: Path,
    project_name: str,
    server_origin: str,
    wdio_spec: str = "test/specs/source-lifecycle.e2e.ts",
    keep_wdio_phase_status: bool = False,
    environ: Mapping[str, str],
) -> LiveAcceptanceConfig:
    """Load non-secret runtime names from the authoritative local launcher."""
    launcher_exports = _literal_launcher_exports(repository_root)
    runtime_environment = {
        key: value for key, value in environ.items() if not key.startswith("KNOWLEDGE_")
    }
    runtime_environment.update(launcher_exports)
    secret_root = (repository_root / ".local" / "stack-secrets").resolve()
    runtime_environment.update(
        {
            "CI": environ.get("CI", ""),
            "KNOWLEDGE_ENVIRONMENT": "local",
            "KNOWLEDGE_SECRET_ROOT": str(secret_root),
        }
    )
    return LiveAcceptanceConfig(
        repository_root=repository_root,
        project_name=project_name,
        username="duc",
        workspace_key="duc-knowledge",
        server_origin=server_origin,
        allowed_origin=launcher_exports["KNOWLEDGE_AUTH_ALLOWED_ORIGIN"],
        wdio_spec=wdio_spec,
        keep_wdio_phase_status=keep_wdio_phase_status,
        password_file=secret_root / "web-credential-password.key",
        runtime_environment=runtime_environment,
        policy_key_file_name=launcher_exports["KNOWLEDGE_POLICY_SIGNING_KEY_FILE"],
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap one disposable identity and run live Obsidian acceptance."
    )
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--server-origin", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--wdio-spec",
        choices=(
            "test/specs/source-lifecycle.e2e.ts",
            "test/specs/device-login-sync.e2e.ts",
            "test/specs/device-sync-reconciliation.e2e.ts",
            "test/specs/multipart-upload.e2e.ts",
            "test/specs/diagnostics-surface-live-smoke.e2e.ts",
        ),
        default="test/specs/source-lifecycle.e2e.ts",
    )
    parser.add_argument("--keep-wdio-phase-status", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    try:
        config = build_live_acceptance_config(
            repository_root=repository_root,
            project_name=cast(str, arguments.project_name),
            server_origin=cast(str, arguments.server_origin),
            wdio_spec=cast(str, arguments.wdio_spec),
            keep_wdio_phase_status=bool(arguments.keep_wdio_phase_status),
            environ=os.environ,
        )
    except LiveAcceptanceFailure as failure:
        _print_status(sys.stdout, "error", failure.result_code)
        return 1
    return run_live_acceptance(
        config,
        executor=SubprocessCommandExecutor(),
        client_factory=lambda: httpx.Client(
            base_url=config.server_origin,
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        ),
        output=sys.stdout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
