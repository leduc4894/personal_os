"""Executable contract for the disposable Obsidian live bootstrap."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from io import StringIO
from pathlib import Path
from typing import Final

import httpx
import pytest
import tools.obsidian_live_acceptance_bootstrap as live_bootstrap
from tools.obsidian_live_acceptance_bootstrap import (
    CommandResult,
    LiveAcceptanceConfig,
    build_live_acceptance_config,
    run_live_acceptance,
)

_WORKSPACE_ID: Final = "018f47b1-8a44-7a21-bf19-6b2748c90861"
_TOTP_SECRET_SENTINEL: Final = "JBSWY3DPEHPK3PXP"
_PASSWORD_SENTINEL: Final = "correct horse battery staple!"
_RECOVERY_SENTINEL: Final = "ABCD-EFGH-JKLM"
_CHILD_OUTPUT_SENTINEL: Final = "child-private-output"


@pytest.fixture(autouse=True)
def _advance_totp_waits_without_wall_clock_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"unix_time_seconds": 120.0}
    monkeypatch.setattr(
        live_bootstrap.time,
        "time",
        lambda: clock["unix_time_seconds"],
    )

    def advance_clock(seconds: float) -> None:
        clock["unix_time_seconds"] += seconds

    monkeypatch.setattr(live_bootstrap.time, "sleep", advance_clock)


class FreshDisposableExecutor:
    """Controlled external boundary whose helper becomes active after HTTP verify."""

    def __init__(
        self,
        events: list[str],
        state: dict[str, bool],
        *,
        is_credential_enrolled: bool = False,
        malformed_preflight_before_activation: bool = False,
    ) -> None:
        self.events = events
        self.state = state
        self.is_credential_enrolled = is_credential_enrolled
        self.malformed_preflight_before_activation = malformed_preflight_before_activation

    def run(
        self,
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str],
        cwd: Path,
        timeout_seconds: float,
        should_capture: bool,
    ) -> CommandResult:
        del environment, cwd, timeout_seconds, should_capture
        command = " ".join(arguments)
        if "local_service_stack.py status" in command:
            self.events.append("stack_ready")
            return CommandResult(0, '{"state":"ready"}', "")
        if "alembic upgrade head" in command:
            self.events.append("migration_applied")
            return CommandResult(0, _CHILD_OUTPUT_SENTINEL, _CHILD_OUTPUT_SENTINEL)
        if "canonical_core_operations.py bootstrap-identity" in command:
            self.events.append("identity_ready")
            return CommandResult(
                0,
                json.dumps(
                    {
                        "result_code": "identity_bootstrap_created",
                        "workspace_id": _WORKSPACE_ID,
                    }
                ),
                _CHILD_OUTPUT_SENTINEL,
            )
        if "web-credential-status" in command:
            self.events.append("credential_status")
            if self.is_credential_enrolled:
                return CommandResult(0, "enrolled=true credential_revision=1", "")
            return CommandResult(0, "enrolled=false credential_revision=none", "")
        if "enroll-web-credential" in command:
            self.events.append("credential_enrolled")
            return CommandResult(0, _CHILD_OUTPUT_SENTINEL, _CHILD_OUTPUT_SENTINEL)
        if "policy-key initialize" in command:
            self.events.append("policy_key_ready")
            return CommandResult(0, _CHILD_OUTPUT_SENTINEL, _CHILD_OUTPUT_SENTINEL)
        if ".local/e2e-totp-code.py" in command:
            if not self.state["totp_active"]:
                self.events.append("totp_preflight_missing")
                if self.malformed_preflight_before_activation:
                    return CommandResult(0, "not-a-totp-code", _CHILD_OUTPUT_SENTINEL)
                return CommandResult(1, "", "no active totp credential found")
            self.events.append("totp_preflight_active")
            return CommandResult(0, "123456", _CHILD_OUTPUT_SENTINEL)
        if ".local/publish-policy-revision.py" in command:
            self.events.append("policy_published")
            return CommandResult(0, _CHILD_OUTPUT_SENTINEL, _CHILD_OUTPUT_SENTINEL)
        if "wdio run" in command:
            self.events.append("wdio_started")
            return CommandResult(0, _CHILD_OUTPUT_SENTINEL, _CHILD_OUTPUT_SENTINEL)
        raise AssertionError(f"unexpected command shape: {arguments[0]}")


def test_fresh_disposable_bootstrap_activates_totp_before_live_journey(
    tmp_path: Path,
) -> None:
    """A missing helper result must select HTTP activation, never BLOCKED."""
    password_file = tmp_path / "web-credential-password.key"
    password_file.write_text(_PASSWORD_SENTINEL, encoding="utf-8")
    events: list[str] = []
    state = {"totp_active": False}

    def http_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path == "/api/auth/login":
            events.append("login")
            assert payload == {"username": "duc", "password": _PASSWORD_SENTINEL}
            return httpx.Response(
                200,
                json={"data": {"state": "active"}},
                headers=[
                    ("set-cookie", "__Host-session=session-private; Secure; Path=/"),
                    ("set-cookie", "__Host-csrf=csrf-private; Secure; Path=/"),
                ],
            )
        if request.url.path == "/api/auth/totp/enrollments":
            events.append("enrollment_started")
            assert payload == {"action": "start"}
            return httpx.Response(
                200,
                json={
                    "data": {
                        "action": "start",
                        "enrollment": {
                            "enrollment_id": "018f47b1-8a44-7a21-bf19-6b2748c90862",
                            "provisioning_uri": "otpauth://totp/private",
                            "secret": _TOTP_SECRET_SENTINEL,
                            "expires_at": "2030-01-01T00:00:00Z",
                        },
                    }
                },
            )
        if request.url.path.endswith("/verify"):
            events.append("enrollment_verified")
            assert set(payload) == {"code"}
            assert isinstance(payload["code"], str)
            assert len(payload["code"]) == 6
            state["totp_active"] = True
            return httpx.Response(
                200,
                json={"data": {"codes": [_RECOVERY_SENTINEL], "revision": 1}},
            )
        raise AssertionError(f"unexpected HTTP path: {request.url.path}")

    config = LiveAcceptanceConfig(
        repository_root=tmp_path,
        project_name="knowledge-ci-live-acceptance",
        username="duc",
        workspace_key="duc-knowledge",
        server_origin="http://127.0.0.1:8000",
        allowed_origin="https://app.example.test",
        plugin_origin="https://api.example.test",
        password_file=password_file,
        runtime_environment={"CI": "true", "KNOWLEDGE_ENVIRONMENT": "local"},
    )
    output = StringIO()
    executor = FreshDisposableExecutor(events, state)

    exit_code = run_live_acceptance(
        config,
        executor=executor,
        client_factory=lambda: httpx.Client(
            base_url=config.server_origin,
            transport=httpx.MockTransport(http_handler),
            timeout=5,
        ),
        output=output,
    )

    assert exit_code == 0
    result_path = tmp_path / ".local" / "knowledge-ci-live-acceptance.obsidian-live-result.json"
    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "result_code": "obsidian_live_acceptance_passed",
        "state": "complete",
        "wdio_phase": None,
    }
    assert events == [
        "stack_ready",
        "migration_applied",
        "identity_ready",
        "credential_status",
        "credential_enrolled",
        "policy_key_ready",
        "totp_preflight_missing",
        "login",
        "enrollment_started",
        "enrollment_verified",
        "totp_preflight_active",
        "policy_published",
        "wdio_started",
    ]
    rendered = output.getvalue()
    assert json.loads(rendered) == {
        "result_code": "obsidian_live_acceptance_passed",
        "state": "complete",
    }
    for private_value in (
        _TOTP_SECRET_SENTINEL,
        _PASSWORD_SENTINEL,
        _RECOVERY_SENTINEL,
        _CHILD_OUTPUT_SENTINEL,
        "session-private",
        "csrf-private",
        "123456",
    ):
        assert private_value not in rendered


def test_fresh_activation_uses_a_new_totp_step_for_policy_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The activation code is replay-locked and cannot authenticate policy."""
    password_file = tmp_path / "web-credential-password.key"
    password_file.write_text(_PASSWORD_SENTINEL, encoding="utf-8")
    events: list[str] = []
    state = {"totp_active": False}
    clock = {"unix_time_seconds": 120.0}
    monkeypatch.setattr(
        live_bootstrap.time,
        "time",
        lambda: clock["unix_time_seconds"],
    )

    def advance_clock(seconds: float) -> None:
        clock["unix_time_seconds"] += seconds

    monkeypatch.setattr(live_bootstrap.time, "sleep", advance_clock)

    def http_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                json={"data": {"state": "active"}},
                headers=[
                    ("set-cookie", "__Host-session=private; Secure; Path=/"),
                    ("set-cookie", "__Host-csrf=private; Secure; Path=/"),
                ],
            )
        if request.url.path == "/api/auth/totp/enrollments":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "enrollment": {
                            "enrollment_id": "018f47b1-8a44-7a21-bf19-6b2748c90862",
                            "secret": _TOTP_SECRET_SENTINEL,
                        }
                    }
                },
            )
        if request.url.path.endswith("/verify"):
            state["totp_active"] = True
            events.append("enrollment_verified")
            return httpx.Response(200, json={"data": {"codes": [_RECOVERY_SENTINEL]}})
        raise AssertionError("unexpected HTTP path")

    class ReplayRejectingExecutor(FreshDisposableExecutor):
        def run(
            self,
            arguments: Sequence[str],
            *,
            environment: Mapping[str, str],
            cwd: Path,
            timeout_seconds: float,
            should_capture: bool,
        ) -> CommandResult:
            if (
                ".local/publish-policy-revision.py" in " ".join(arguments)
                and int(clock["unix_time_seconds"]) // 30 == 4
            ):
                self.events.append("policy_replay_rejected")
                return CommandResult(1, "", "")
            return super().run(
                arguments,
                environment=environment,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                should_capture=should_capture,
            )

    config = LiveAcceptanceConfig(
        repository_root=tmp_path,
        project_name="knowledge-ci-fresh-totp-step",
        username="duc",
        workspace_key="duc-knowledge",
        server_origin="http://127.0.0.1:8000",
        allowed_origin="https://app.example.test",
        plugin_origin="https://api.example.test",
        password_file=password_file,
        runtime_environment={"CI": "true", "KNOWLEDGE_ENVIRONMENT": "local"},
    )
    output = StringIO()

    exit_code = run_live_acceptance(
        config,
        executor=ReplayRejectingExecutor(events, state),
        client_factory=lambda: httpx.Client(
            base_url=config.server_origin,
            transport=httpx.MockTransport(http_handler),
        ),
        output=output,
    )

    assert exit_code == 0
    assert "policy_replay_rejected" not in events
    assert events.index("enrollment_verified") < events.index("policy_published")
    assert json.loads(output.getvalue()) == {
        "result_code": "obsidian_live_acceptance_passed",
        "state": "complete",
    }


def test_malformed_totp_helper_output_runs_enrollment_before_wdio(tmp_path: Path) -> None:
    """Exit zero alone is not active-TOTP evidence; the code shape is closed."""
    password_file = tmp_path / "web-credential-password.key"
    password_file.write_text(_PASSWORD_SENTINEL, encoding="utf-8")
    events: list[str] = []
    state = {"totp_active": False}

    def http_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            events.append("login")
            return httpx.Response(
                200,
                json={"data": {"state": "active"}},
                headers=[
                    ("set-cookie", "__Host-session=private; Secure; Path=/"),
                    ("set-cookie", "__Host-csrf=private; Secure; Path=/"),
                ],
            )
        if request.url.path == "/api/auth/totp/enrollments":
            events.append("enrollment_started")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "enrollment": {
                            "enrollment_id": "018f47b1-8a44-7a21-bf19-6b2748c90862",
                            "secret": _TOTP_SECRET_SENTINEL,
                        }
                    }
                },
            )
        if request.url.path.endswith("/verify"):
            events.append("enrollment_verified")
            state["totp_active"] = True
            return httpx.Response(200, json={"data": {"codes": [_RECOVERY_SENTINEL]}})
        raise AssertionError("unexpected HTTP path")

    config = LiveAcceptanceConfig(
        repository_root=tmp_path,
        project_name="knowledge-ci-malformed-helper",
        username="duc",
        workspace_key="duc-knowledge",
        server_origin="http://127.0.0.1:8000",
        allowed_origin="https://app.example.test",
        plugin_origin="https://api.example.test",
        password_file=password_file,
        runtime_environment={"CI": "true", "KNOWLEDGE_ENVIRONMENT": "local"},
    )
    output = StringIO()

    exit_code = run_live_acceptance(
        config,
        executor=FreshDisposableExecutor(
            events,
            state,
            malformed_preflight_before_activation=True,
        ),
        client_factory=lambda: httpx.Client(
            base_url=config.server_origin,
            transport=httpx.MockTransport(http_handler),
        ),
        output=output,
    )

    assert exit_code == 0
    assert events.index("enrollment_started") < events.index("totp_preflight_active")
    assert events.index("totp_preflight_active") < events.index("wdio_started")


def test_active_totp_rerun_skips_enrollment_and_preflights_before_wdio(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "web-credential-password.key"
    password_file.write_text(_PASSWORD_SENTINEL, encoding="utf-8")
    events: list[str] = []
    config = LiveAcceptanceConfig(
        repository_root=tmp_path,
        project_name="knowledge-ci-active-rerun",
        username="duc",
        workspace_key="duc-knowledge",
        server_origin="http://127.0.0.1:8000",
        allowed_origin="https://app.example.test",
        plugin_origin="https://api.example.test",
        password_file=password_file,
        runtime_environment={"CI": "true", "KNOWLEDGE_ENVIRONMENT": "local"},
    )

    exit_code = run_live_acceptance(
        config,
        executor=FreshDisposableExecutor(
            events,
            {"totp_active": True},
            is_credential_enrolled=True,
        ),
        client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("active TOTP must not start enrollment")
        ),
        output=StringIO(),
    )

    assert exit_code == 0
    assert "login" not in events
    assert events.count("totp_preflight_active") == 1
    assert events.index("totp_preflight_active") < events.index("policy_published")
    assert events.index("policy_published") < events.index("wdio_started")


def test_policy_publication_uses_an_older_totp_step_than_wdio_onboarding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy consumes one code; WDIO onboarding must receive a newer code."""
    password_file = tmp_path / "web-credential-password.key"
    password_file.write_text(_PASSWORD_SENTINEL, encoding="utf-8")
    events: list[str] = []
    clock = {"unix_time_seconds": 120.0}
    monkeypatch.setattr(
        live_bootstrap.time,
        "time",
        lambda: clock["unix_time_seconds"],
    )

    def advance_clock(seconds: float) -> None:
        clock["unix_time_seconds"] += seconds

    monkeypatch.setattr(live_bootstrap.time, "sleep", advance_clock)

    class TotpReplayRejectingExecutor(FreshDisposableExecutor):
        policy_time_step: int | None = None

        def run(
            self,
            arguments: Sequence[str],
            *,
            environment: Mapping[str, str],
            cwd: Path,
            timeout_seconds: float,
            should_capture: bool,
        ) -> CommandResult:
            command = " ".join(arguments)
            current_time_step = int(clock["unix_time_seconds"]) // 30
            if ".local/publish-policy-revision.py" in command:
                self.policy_time_step = current_time_step
            if "wdio run" in command and self.policy_time_step == current_time_step:
                self.events.append("wdio_totp_replay_rejected")
                return CommandResult(1, "", "")
            return super().run(
                arguments,
                environment=environment,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                should_capture=should_capture,
            )

    config = LiveAcceptanceConfig(
        repository_root=tmp_path,
        project_name="knowledge-ci-policy-wdio-step",
        username="duc",
        workspace_key="duc-knowledge",
        server_origin="http://127.0.0.1:8000",
        allowed_origin="https://app.example.test",
        plugin_origin="https://api.example.test",
        password_file=password_file,
        runtime_environment={"CI": "true", "KNOWLEDGE_ENVIRONMENT": "local"},
    )
    output = StringIO()

    exit_code = run_live_acceptance(
        config,
        executor=TotpReplayRejectingExecutor(
            events,
            {"totp_active": True},
            is_credential_enrolled=True,
        ),
        client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("active TOTP must not start enrollment")
        ),
        output=output,
    )

    assert exit_code == 0
    assert "wdio_totp_replay_rejected" not in events
    assert events.index("policy_published") < events.index("wdio_started")


def test_failed_post_enrollment_preflight_never_publishes_or_runs_wdio(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "web-credential-password.key"
    password_file.write_text(_PASSWORD_SENTINEL, encoding="utf-8")
    events: list[str] = []
    state = {"totp_active": False}

    def http_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                json={"data": {"state": "active"}},
                headers=[
                    ("set-cookie", "__Host-session=private; Secure; Path=/"),
                    ("set-cookie", "__Host-csrf=private; Secure; Path=/"),
                ],
            )
        if request.url.path == "/api/auth/totp/enrollments":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "enrollment": {
                            "enrollment_id": "018f47b1-8a44-7a21-bf19-6b2748c90862",
                            "secret": _TOTP_SECRET_SENTINEL,
                        }
                    }
                },
            )
        if request.url.path.endswith("/verify"):
            return httpx.Response(200, json={"data": {"codes": [_RECOVERY_SENTINEL]}})
        raise AssertionError("unexpected HTTP path")

    config = LiveAcceptanceConfig(
        repository_root=tmp_path,
        project_name="knowledge-ci-postflight-failure",
        username="duc",
        workspace_key="duc-knowledge",
        server_origin="http://127.0.0.1:8000",
        allowed_origin="https://app.example.test",
        plugin_origin="https://api.example.test",
        password_file=password_file,
        runtime_environment={"CI": "true", "KNOWLEDGE_ENVIRONMENT": "local"},
    )
    output = StringIO()

    exit_code = run_live_acceptance(
        config,
        executor=FreshDisposableExecutor(events, state),
        client_factory=lambda: httpx.Client(
            base_url=config.server_origin,
            transport=httpx.MockTransport(http_handler),
        ),
        output=output,
    )

    assert exit_code == 1
    assert json.loads(output.getvalue()) == {
        "result_code": "active_totp_preflight_failed",
        "state": "error",
    }
    assert "policy_published" not in events
    assert "wdio_started" not in events


def test_invalid_project_fails_before_any_external_operation(tmp_path: Path) -> None:
    class NoExternalOperations:
        def run(
            self,
            arguments: Sequence[str],
            *,
            environment: Mapping[str, str],
            cwd: Path,
            timeout_seconds: float,
            should_capture: bool,
        ) -> CommandResult:
            del arguments, environment, cwd, timeout_seconds, should_capture
            raise AssertionError("invalid input must fail before external I/O")

    output = StringIO()
    exit_code = run_live_acceptance(
        LiveAcceptanceConfig(
            repository_root=tmp_path,
            project_name="knowledge-local",
            username="duc",
            workspace_key="duc-knowledge",
            server_origin="http://127.0.0.1:8000",
            allowed_origin="https://app.example.test",
            plugin_origin="https://api.example.test",
            password_file=tmp_path / "unused",
            runtime_environment={"CI": "true"},
        ),
        executor=NoExternalOperations(),
        client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("invalid input must fail before HTTP")
        ),
        output=output,
    )

    assert exit_code == 1
    assert json.loads(output.getvalue()) == {
        "result_code": "disposable_project_required",
        "state": "error",
    }


def test_launcher_settings_reach_totp_policy_and_wdio_children(tmp_path: Path) -> None:
    """The single entrypoint must close the prior missing-settings failure."""
    local_directory = tmp_path / ".local"
    secret_root = local_directory / "stack-secrets"
    secret_root.mkdir(parents=True)
    (secret_root / "web-credential-password.key").write_text(_PASSWORD_SENTINEL, encoding="utf-8")
    launcher_values = {
        "KNOWLEDGE_DATABASE_HOST": "127.0.0.1",
        "KNOWLEDGE_DATABASE_PORT": "5432",
        "KNOWLEDGE_DATABASE_NAME": "knowledge",
        "KNOWLEDGE_DATABASE_USER": "knowledge_app",
        "KNOWLEDGE_DATABASE_PASSWORD_FILE": "postgres_application_password",
        "KNOWLEDGE_DATABASE_SSL_MODE": "disable",
        "KNOWLEDGE_AUTH_ALLOWED_ORIGIN": "https://admin.example.test",
        "KNOWLEDGE_AUTH_CURRENT_KEY_ID": "auth-key-live",
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "auth-key-live.key",
        "KNOWLEDGE_AUTH_MIN_PLUGIN_VERSION": "0.1.0",
        "KNOWLEDGE_AUTH_MAX_PLUGIN_VERSION": "0.1.0",
        "KNOWLEDGE_POLICY_SIGNING_KEY_ID": "policy-key-live",
        "KNOWLEDGE_POLICY_SIGNING_KEY_FILE": "policy-key-live.pem",
    }
    (local_directory / "serve-local.sh").write_text(
        "\n".join(f'export {name}="{value}"' for name, value in launcher_values.items()),
        encoding="utf-8",
    )
    config = build_live_acceptance_config(
        repository_root=tmp_path,
        project_name="knowledge-ci-loaded-environment",
        server_origin="http://127.0.0.1:8010",
        plugin_origin="https://plugin.example.test",
        environ={"CI": "true", "PATH": "test-path"},
    )
    observed_environments: dict[str, Mapping[str, str]] = {}
    events: list[str] = []

    class EnvironmentCapturingExecutor(FreshDisposableExecutor):
        def run(
            self,
            arguments: Sequence[str],
            *,
            environment: Mapping[str, str],
            cwd: Path,
            timeout_seconds: float,
            should_capture: bool,
        ) -> CommandResult:
            command = " ".join(arguments)
            if ".local/e2e-totp-code.py" in command:
                observed_environments["totp"] = dict(environment)
            if ".local/publish-policy-revision.py" in command:
                observed_environments["policy"] = dict(environment)
            if "wdio run" in command:
                observed_environments["wdio"] = dict(environment)
            return super().run(
                arguments,
                environment=environment,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                should_capture=should_capture,
            )

    exit_code = run_live_acceptance(
        config,
        executor=EnvironmentCapturingExecutor(
            events,
            {"totp_active": True},
            is_credential_enrolled=True,
        ),
        client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("active TOTP must not start enrollment")
        ),
        output=StringIO(),
    )

    assert exit_code == 0
    required_helper_names = {
        "KNOWLEDGE_SECRET_ROOT",
        "KNOWLEDGE_DATABASE_HOST",
        "KNOWLEDGE_DATABASE_PORT",
        "KNOWLEDGE_DATABASE_NAME",
        "KNOWLEDGE_DATABASE_USER",
        "KNOWLEDGE_DATABASE_PASSWORD_FILE",
        "KNOWLEDGE_AUTH_ALLOWED_ORIGIN",
        "KNOWLEDGE_AUTH_CURRENT_KEY_ID",
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE",
        "KNOWLEDGE_AUTH_MIN_PLUGIN_VERSION",
        "KNOWLEDGE_AUTH_MAX_PLUGIN_VERSION",
    }
    assert required_helper_names.issubset(observed_environments["totp"])
    expected_e2e_environment = {
        "E2E_SERVER_ORIGIN": "http://127.0.0.1:8010",
        "E2E_ALLOWED_ORIGIN": "https://admin.example.test",
        "E2E_PLUGIN_ORIGIN": "https://plugin.example.test",
        "E2E_WEB_USERNAME": "duc",
        "E2E_WEB_PASSWORD_FILE": str(secret_root.resolve() / "web-credential-password.key"),
        "E2E_TOTP_HELPER": ".local/e2e-totp-code.py",
    }
    for child_name in ("policy", "wdio"):
        for name, value in expected_e2e_environment.items():
            assert observed_environments[child_name][name] == value


@pytest.mark.parametrize(
    ("phase_code", "expected_failure_code"),
    (
        ("source_lifecycle_move_completed", "obsidian_wdio_failed_after_move"),
        (
            "automatic_existing_note_committed",
            "obsidian_wdio_failed_after_automatic_existing_note_commit",
        ),
        (
            "automatic_new_note_committed",
            "obsidian_wdio_failed_after_automatic_new_note_commit",
        ),
        (
            "automatic_policy_successor_committed",
            "obsidian_wdio_failed_after_automatic_policy_successor_commit",
        ),
        (
            "automatic_convergence_journey_completed",
            "obsidian_wdio_failed_after_automatic_convergence_journey",
        ),
    ),
)
def test_failed_wdio_reports_the_last_closed_lifecycle_phase(
    tmp_path: Path,
    phase_code: str,
    expected_failure_code: str,
) -> None:
    """A closed phase marker distinguishes scenario progress without child output."""
    password_file = tmp_path / "web-credential-password.key"
    password_file.write_text(_PASSWORD_SENTINEL, encoding="utf-8")
    events: list[str] = []
    status_paths: list[Path] = []

    class PhaseReportingExecutor(FreshDisposableExecutor):
        def run(
            self,
            arguments: Sequence[str],
            *,
            environment: Mapping[str, str],
            cwd: Path,
            timeout_seconds: float,
            should_capture: bool,
        ) -> CommandResult:
            if "wdio run" in " ".join(arguments):
                status_file = Path(environment["E2E_LIVE_PHASE_STATUS_FILE"])
                assert status_file.name == "knowledge-ci-phase-report.obsidian-live-phase.json"
                status_file.parent.mkdir()
                status_paths.append(status_file)
                status_file.write_text(
                    json.dumps({"result_code": phase_code}),
                    encoding="utf-8",
                )
                status_file.with_name(f"{status_file.name}.diagnostic.json").write_text(
                    json.dumps({"committedCount": 1}),
                    encoding="utf-8",
                )
                return CommandResult(1, _CHILD_OUTPUT_SENTINEL, _CHILD_OUTPUT_SENTINEL)
            return super().run(
                arguments,
                environment=environment,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                should_capture=should_capture,
            )

    output = StringIO()
    exit_code = run_live_acceptance(
        LiveAcceptanceConfig(
            repository_root=tmp_path,
            project_name="knowledge-ci-phase-report",
            username="duc",
            workspace_key="duc-knowledge",
            server_origin="http://127.0.0.1:8000",
            allowed_origin="https://app.example.test",
            plugin_origin="https://api.example.test",
            password_file=password_file,
            runtime_environment={"CI": "true", "KNOWLEDGE_ENVIRONMENT": "local"},
        ),
        executor=PhaseReportingExecutor(
            events,
            {"totp_active": True},
            is_credential_enrolled=True,
        ),
        client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("active TOTP must not start enrollment")
        ),
        output=output,
    )

    assert exit_code == 1
    assert json.loads(output.getvalue()) == {
        "result_code": expected_failure_code,
        "state": "error",
    }
    assert _CHILD_OUTPUT_SENTINEL not in output.getvalue()
    assert len(status_paths) == 1
    assert not status_paths[0].exists()
    assert not status_paths[0].with_name(f"{status_paths[0].name}.diagnostic.json").exists()


@pytest.mark.parametrize(
    "unsafe_status",
    (
        "not-json",
        json.dumps(
            {
                "result_code": "source_lifecycle_move_completed",
                "diagnostic": _CHILD_OUTPUT_SENTINEL,
            }
        ),
        json.dumps({"result_code": _CHILD_OUTPUT_SENTINEL}),
    ),
)
def test_failed_wdio_maps_malformed_or_unsafe_phase_status_to_generic_code(
    tmp_path: Path,
    unsafe_status: str,
) -> None:
    """Untrusted marker content is never forwarded through the parent boundary."""
    password_file = tmp_path / "web-credential-password.key"
    password_file.write_text(_PASSWORD_SENTINEL, encoding="utf-8")

    class UnsafePhaseExecutor(FreshDisposableExecutor):
        def run(
            self,
            arguments: Sequence[str],
            *,
            environment: Mapping[str, str],
            cwd: Path,
            timeout_seconds: float,
            should_capture: bool,
        ) -> CommandResult:
            if "wdio run" in " ".join(arguments):
                status_file = Path(environment["E2E_LIVE_PHASE_STATUS_FILE"])
                status_file.parent.mkdir()
                status_file.write_text(
                    unsafe_status,
                    encoding="utf-8",
                )
                return CommandResult(1, _CHILD_OUTPUT_SENTINEL, _CHILD_OUTPUT_SENTINEL)
            return super().run(
                arguments,
                environment=environment,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                should_capture=should_capture,
            )

    output = StringIO()
    exit_code = run_live_acceptance(
        LiveAcceptanceConfig(
            repository_root=tmp_path,
            project_name="knowledge-ci-unsafe-phase",
            username="duc",
            workspace_key="duc-knowledge",
            server_origin="http://127.0.0.1:8000",
            allowed_origin="https://app.example.test",
            plugin_origin="https://api.example.test",
            password_file=password_file,
            runtime_environment={"CI": "true", "KNOWLEDGE_ENVIRONMENT": "local"},
        ),
        executor=UnsafePhaseExecutor(
            [],
            {"totp_active": True},
            is_credential_enrolled=True,
        ),
        client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("active TOTP must not start enrollment")
        ),
        output=output,
    )

    assert exit_code == 1
    assert json.loads(output.getvalue()) == {
        "result_code": "obsidian_wdio_failed",
        "state": "error",
    }
    assert _CHILD_OUTPUT_SENTINEL not in output.getvalue()
