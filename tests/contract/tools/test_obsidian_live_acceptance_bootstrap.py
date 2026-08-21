"""Executable contract for the disposable Obsidian live bootstrap."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from io import StringIO
from pathlib import Path
from typing import Final

import httpx
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
