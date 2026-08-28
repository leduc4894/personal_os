"""API server runner: approved Uvicorn flags, snapshot isolation and safe exits.

These tests prove the :func:`run_server` contract against a recording server
factory that never binds a socket: the single-process Uvicorn configuration
(loopback-or-explicit host and port, no server header, no proxy headers, no
reload, exactly one worker), the application wiring for the configured
environment, and the closed exit-code mapping — configuration and secret
failures exit ``78`` with one safe emergency record, unexpected startup
failures exit ``70``, and a clean shutdown exits ``0``. Raw exception text and
secret paths never reach the output.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

import pytest
import uvicorn
from api_runtime.server import run_server
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from fastapi import FastAPI

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.exclusion_policy.signatures import derive_ed25519_key_id

# One hermetic secret root backing the local environment snapshot, so the
# password, authentication key and policy signing key reads inside run_server
# succeed without touching process secrets.
_SECRET_DIRECTORY = tempfile.TemporaryDirectory(prefix="api-runtime-server-secrets-")
atexit.register(_SECRET_DIRECTORY.cleanup)
_SECRET_ROOT = Path(_SECRET_DIRECTORY.name)
(_SECRET_ROOT / "postgres_application_password").write_text(
    "server-test-password\n", encoding="utf-8"
)
(_SECRET_ROOT / "authentication_current_key").write_text("00" * 32 + "\n", encoding="utf-8")
_POLICY_SIGNING_KEY = Ed25519PrivateKey.generate()
(_SECRET_ROOT / "policy_signing_current.pem").write_bytes(
    _POLICY_SIGNING_KEY.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
)
_POLICY_SIGNING_KEY_ID = derive_ed25519_key_id(_POLICY_SIGNING_KEY.public_key().public_bytes_raw())
(_SECRET_ROOT / "r2_access_key_id").write_text("test-access-key-id" + chr(10), encoding="utf-8")
(_SECRET_ROOT / "r2_secret_access_key").write_text(
    "test-secret-access-key" + chr(10), encoding="utf-8"
)
_SPOOL_DIRECTORY = tempfile.TemporaryDirectory(prefix="api-runtime-server-spool-")
atexit.register(_SPOOL_DIRECTORY.cleanup)
_SPOOL_ROOT = Path(_SPOOL_DIRECTORY.name)

LOCAL_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        "KNOWLEDGE_ENVIRONMENT": "local",
        "KNOWLEDGE_SECRET_ROOT": str(_SECRET_ROOT),
        "KNOWLEDGE_AUTH_ALLOWED_ORIGIN": "http://127.0.0.1:8000",
        "KNOWLEDGE_AUTH_CURRENT_KEY_ID": "auth-key-v1",
        "KNOWLEDGE_AUTH_CURRENT_KEY_FILE": "authentication_current_key",
        "KNOWLEDGE_AUTH_MIN_PLUGIN_VERSION": "1.13.1",
        "KNOWLEDGE_AUTH_MAX_PLUGIN_VERSION": "1.20.0",
        "KNOWLEDGE_POLICY_SIGNING_KEY_ID": _POLICY_SIGNING_KEY_ID,
        "KNOWLEDGE_POLICY_SIGNING_KEY_FILE": "policy_signing_current.pem",
        "KNOWLEDGE_R2_ENDPOINT": f"https://{'0' * 32}.r2.cloudflarestorage.com",
        "KNOWLEDGE_R2_BUCKET_NAME": "personal-knowledge-objects",
        "KNOWLEDGE_R2_ACCESS_KEY_ID_FILE": "r2_access_key_id",
        "KNOWLEDGE_R2_SECRET_ACCESS_KEY_FILE": "r2_secret_access_key",
        "KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT": str(_SPOOL_ROOT),
    }
)

_SENTINEL_HOSTILE_DETAIL = "do-not-emit-hostile-exception-detail"


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """Undo the root handler installation performed by configure_diagnostics."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers = handlers
    root.setLevel(level)


class RecordingServer:
    """Server stand-in exposing the prepared config and a clean shutdown."""

    def __init__(self, config: uvicorn.Config) -> None:
        self.config = config
        self.observed_loop: asyncio.AbstractEventLoop | None = None

    async def serve(self) -> None:
        """Simulate an immediately clean shutdown without binding a socket."""
        self.observed_loop = asyncio.get_running_loop()
        return None


class RecordingServerFactory:
    """Factory capturing the single Uvicorn config the runner prepared."""

    def __init__(self) -> None:
        self.config: uvicorn.Config | None = None
        self.last_server: RecordingServer | None = None

    def __call__(self, config: uvicorn.Config) -> RecordingServer:
        assert self.config is None, "run_server must prepare exactly one server"
        self.config = config
        server = RecordingServer(config)
        self.last_server = server
        return server


class ExplodingServerFactory:
    """Factory failing startup after configuration, with hostile detail text."""

    def __call__(self, config: uvicorn.Config) -> RecordingServer:
        del config
        raise RuntimeError(_SENTINEL_HOSTILE_DETAIL)


class ExitingServer(RecordingServer):
    """Server whose serve raises ``SystemExit`` like Uvicorn on bind failure."""

    def __init__(self, exit_code: int | None, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.exit_code = exit_code

    async def serve(self) -> None:
        raise SystemExit(self.exit_code)


class ExitingServerFactory:
    """Factory returning a server whose run exits with the prepared code."""

    def __init__(self, exit_code: int | None) -> None:
        self.exit_code = exit_code

    def __call__(self, config: uvicorn.Config) -> ExitingServer:
        return ExitingServer(self.exit_code, config)


class LifespanExecutingServer(RecordingServer):
    """Server stand-in that executes application startup without binding."""

    async def serve(self) -> None:
        application = self.config.app
        assert isinstance(application, FastAPI)
        async with application.router.lifespan_context(application):
            return None


class LifespanExecutingServerFactory:
    """Factory exercising the real FastAPI lifespan boundary."""

    def __call__(self, config: uvicorn.Config) -> LifespanExecutingServer:
        return LifespanExecutingServer(config)


def test_server_disables_version_and_proxy_headers() -> None:
    captured = RecordingServerFactory()
    result = run_server(environ=LOCAL_ENVIRONMENT, server_factory=captured)
    assert result == 0
    assert captured.config.host == "127.0.0.1"
    assert captured.config.port == 8000
    assert captured.config.server_header is False
    assert captured.config.proxy_headers is False
    assert captured.config.reload is False
    assert captured.config.workers == 1
    assert captured.config.access_log is False


def test_server_serves_on_a_selector_event_loop() -> None:
    """The serving loop must be psycopg-async compatible.

    psycopg async connections refuse the Windows Proactor loop, and
    Uvicorn's own loop factory selects ProactorEventLoop on win32; the
    runner therefore must serve the application on a SelectorEventLoop
    itself instead of delegating to ``Server.run``.
    """

    captured = RecordingServerFactory()
    result = run_server(environ=LOCAL_ENVIRONMENT, server_factory=captured)

    assert result == 0
    assert captured.config is not None
    server = captured.last_server
    assert server is not None
    assert isinstance(server.observed_loop, asyncio.SelectorEventLoop)


def test_server_builds_local_application_with_openapi_route() -> None:
    captured = RecordingServerFactory()
    assert run_server(environ=LOCAL_ENVIRONMENT, server_factory=captured) == 0
    application = captured.config.app
    assert isinstance(application, FastAPI)
    paths = {route.path for route in application.routes}
    assert "/api/health/live" in paths
    assert "/api/health/ready" in paths
    assert "/api/openapi.json" in paths
    assert "/api/auth/login" in paths
    assert "/api/auth/session" in paths
    assert "/api/auth/logout" in paths
    assert "/api/auth/reauthenticate" in paths
    assert "/api/auth/password" in paths


def test_server_missing_authentication_key_file_exits_seventy_eight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment: dict[str, str] = dict(LOCAL_ENVIRONMENT)
    environment["KNOWLEDGE_AUTH_CURRENT_KEY_FILE"] = "missing_authentication_key"
    result = run_server(environ=environment, server_factory=RecordingServerFactory())
    assert result == 78
    captured = capsys.readouterr()
    assert "runtime_configuration_failed" in captured.err
    assert "secret_file_missing" in captured.err
    assert "missing_authentication_key" not in captured.err
    assert "Traceback" not in captured.err


def test_server_malformed_authentication_key_material_exits_seventy_eight(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (_SECRET_ROOT / "malformed_authentication_key").write_text(
        "not-hexadecimal-key-material\n", encoding="utf-8"
    )
    environment: dict[str, str] = dict(LOCAL_ENVIRONMENT)
    environment["KNOWLEDGE_AUTH_CURRENT_KEY_FILE"] = "malformed_authentication_key"
    try:
        result = run_server(environ=environment, server_factory=RecordingServerFactory())
    finally:
        (_SECRET_ROOT / "malformed_authentication_key").unlink()
    assert result == 78
    captured = capsys.readouterr()
    assert "configuration_secret_invalid" in captured.err
    assert "not-hexadecimal-key-material" not in captured.err
    assert "Traceback" not in captured.err


def test_server_serves_the_small_file_sync_routes() -> None:
    """The serve composition binds the real small-file sync runtime.

    The built application must carry the two sync routes of spec 10 — a serve
    process that 404s them would leave the authenticated upload path
    undelivered — and the bound runtime must be the real adapter graph, not
    the offline double.
    """

    captured = RecordingServerFactory()
    assert run_server(environ=LOCAL_ENVIRONMENT, server_factory=captured) == 0
    application = captured.config.app
    assert isinstance(application, FastAPI)
    paths = {route.path for route in application.routes}
    assert "/api/sync/journal-events/preflight" in paths
    assert "/api/uploads/{operation_id}/content" in paths


def test_server_serves_the_source_lifecycle_route() -> None:
    """The production serve graph must bind the authenticated lifecycle API."""

    captured = RecordingServerFactory()
    assert run_server(environ=LOCAL_ENVIRONMENT, server_factory=captured) == 0
    application = captured.config.app
    assert isinstance(application, FastAPI)
    paths = {route.path for route in application.routes}
    assert "/api/sources/lifecycle-events" in paths


def test_server_serves_the_policy_diagnostics_admin_route() -> None:
    """The serve graph must expose the policy metrics to the Web Admin.

    Spec 2026-08-24 C2: without this route the spec-21 policy counters stay
    unreadable in production even once they record.
    """

    captured = RecordingServerFactory()
    assert run_server(environ=LOCAL_ENVIRONMENT, server_factory=captured) == 0
    application = captured.config.app
    assert isinstance(application, FastAPI)
    paths = {route.path for route in application.routes}
    assert "/api/admin/exclusion-policy/diagnostics" in paths


def test_server_binds_one_shared_policy_metrics_sink_at_both_composition_sites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One shared ``InMemoryExclusionPolicyMetrics`` feeds both sites (C2).

    The engine composition (publication outcomes) and the small-file
    composition (enforcement evaluations) must receive the SAME sink object,
    so the Admin diagnostics route observes both counter families of one
    process through one snapshot.
    """

    from api_runtime import server as server_module

    from personal_os.exclusion_policy.metrics import InMemoryExclusionPolicyMetrics

    observed: dict[str, object] = {}
    real_exclusion_policy_composition = server_module.compose_exclusion_policy
    real_small_file_composition = server_module.compose_small_file_sync

    def recording_exclusion_policy_composition(**kwargs: object) -> object:
        observed["exclusion_policy_metrics"] = kwargs.get("metrics")
        return real_exclusion_policy_composition(**kwargs)

    def recording_small_file_composition(**kwargs: object) -> object:
        observed["small_file_sync_metrics"] = kwargs.get("policy_metrics")
        return real_small_file_composition(**kwargs)

    monkeypatch.setattr(
        server_module, "compose_exclusion_policy", recording_exclusion_policy_composition
    )
    monkeypatch.setattr(server_module, "compose_small_file_sync", recording_small_file_composition)

    captured = RecordingServerFactory()
    assert run_server(environ=LOCAL_ENVIRONMENT, server_factory=captured) == 0

    bound_metrics = observed["exclusion_policy_metrics"]
    assert isinstance(bound_metrics, InMemoryExclusionPolicyMetrics)
    assert observed["small_file_sync_metrics"] is bound_metrics


def test_server_serves_the_multipart_upload_routes() -> None:
    """The production serve graph binds the real multipart upload runtime.

    A serve process that 404s the five spec §5 routes would leave the
    resumable large-file transfer undelivered, and the offline composition
    is for the OpenAPI export and route tests only.
    """

    captured = RecordingServerFactory()
    assert run_server(environ=LOCAL_ENVIRONMENT, server_factory=captured) == 0
    application = captured.config.app
    assert isinstance(application, FastAPI)
    paths = {route.path for route in application.routes}
    assert "/api/uploads/multipart-sessions" in paths
    assert "/api/uploads/multipart-sessions/{session_id}" in paths
    assert "/api/uploads/multipart-sessions/{session_id}/parts/{part_number}/url" in paths
    assert "/api/uploads/multipart-sessions/{session_id}/complete" in paths
    assert "/api/uploads/multipart-sessions/{session_id}/abort" in paths


def test_server_closes_the_multipart_runtime_client_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The serve lifespan disposes the multipart runtime's R2 client once.

    The runtime owns one lazy R2 client manager (the staging provider, the
    staging byte source and the canonical spool share it); the lifespan
    must await its disposal hook exactly once on shutdown, mirroring the
    small-file and device-sync runtimes.
    """

    from dataclasses import replace as dataclass_replace

    from api_runtime import server as server_module
    from api_runtime.multipart_upload_composition import MultipartUploadRuntime

    class _NoDatabaseLifecycle:
        """Lifespan double whose startup opens no PostgreSQL connection."""

        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def check(self) -> None:
            return None

        async def verify_exclusion_policy_signer(self, *, signing_key_id: str) -> None:
            del signing_key_id
            return None

    async def _accept_keyring_coverage(**_: object) -> None:
        return None

    monkeypatch.setattr(server_module, "DatabaseRuntimeLifecycle", _NoDatabaseLifecycle)
    monkeypatch.setattr(
        server_module, "verify_keyring_covers_required_key_ids", _accept_keyring_coverage
    )

    real_composition = server_module.compose_multipart_upload
    close_counts: list[int] = []

    def counting_composition(**kwargs: object) -> MultipartUploadRuntime:
        runtime = real_composition(**kwargs)  # type: ignore[arg-type]
        original_aclose = runtime.aclose
        assert original_aclose is not None

        async def counting_aclose() -> None:
            close_counts.append(1)
            await original_aclose()

        return dataclass_replace(runtime, aclose=counting_aclose)

    monkeypatch.setattr(server_module, "compose_multipart_upload", counting_composition)
    result = run_server(
        environ=LOCAL_ENVIRONMENT,
        server_factory=LifespanExecutingServerFactory(),
    )

    assert result == 0
    assert close_counts == [1]


def test_server_shares_the_policy_metrics_sink_with_the_multipart_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The multipart enforcement records into the shared policy sink (C2)."""

    from api_runtime import server as server_module

    from personal_os.exclusion_policy.metrics import InMemoryExclusionPolicyMetrics

    observed: dict[str, object] = {}
    real_exclusion_policy_composition = server_module.compose_exclusion_policy
    real_multipart_composition = server_module.compose_multipart_upload

    def recording_exclusion_policy_composition(**kwargs: object) -> object:
        observed["exclusion_policy_metrics"] = kwargs.get("metrics")
        return real_exclusion_policy_composition(**kwargs)

    def recording_multipart_composition(**kwargs: object) -> object:
        observed["multipart_policy_metrics"] = kwargs.get("policy_metrics")
        return real_multipart_composition(**kwargs)

    monkeypatch.setattr(
        server_module, "compose_exclusion_policy", recording_exclusion_policy_composition
    )
    monkeypatch.setattr(
        server_module, "compose_multipart_upload", recording_multipart_composition
    )

    captured = RecordingServerFactory()
    assert run_server(environ=LOCAL_ENVIRONMENT, server_factory=captured) == 0

    bound_metrics = observed["exclusion_policy_metrics"]
    assert isinstance(bound_metrics, InMemoryExclusionPolicyMetrics)
    assert observed["multipart_policy_metrics"] is bound_metrics


def test_server_missing_object_storage_configuration_exits_seventy_eight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = {
        key: value
        for key, value in LOCAL_ENVIRONMENT.items()
        if "R2_" not in key and key != "KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT"
    }
    result = run_server(environ=environment, server_factory=RecordingServerFactory())
    assert result == 78
    captured = capsys.readouterr()
    assert "runtime_configuration_failed" in captured.err or "internal_error" in captured.err
    assert "r2" not in captured.err.lower()
    assert "Traceback" not in captured.err


def test_server_uses_the_passed_environment_snapshot_exclusively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNOWLEDGE_API_PORT", "9999")
    monkeypatch.setenv("KNOWLEDGE_API_HOST", "0.0.0.0")
    captured = RecordingServerFactory()
    assert run_server(environ=LOCAL_ENVIRONMENT, server_factory=captured) == 0
    assert captured.config.host == "127.0.0.1"
    assert captured.config.port == 8000


def test_server_configuration_failure_exits_seventy_eight_with_safe_record(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment: Mapping[str, str] = {
        "KNOWLEDGE_ENVIRONMENT": "staging",
        "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
    }
    result = run_server(environ=environment, server_factory=RecordingServerFactory())
    assert result == 78
    captured = capsys.readouterr()
    assert "runtime_configuration_failed" in captured.err
    assert "configuration_invalid" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_server_secret_failure_exits_seventy_eight_without_path_disclosure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_root = tmp_path / "missing-secret-root"
    environment: Mapping[str, str] = {
        "KNOWLEDGE_ENVIRONMENT": "local",
        "KNOWLEDGE_SECRET_ROOT": str(missing_root),
    }
    result = run_server(environ=environment, server_factory=RecordingServerFactory())
    assert result == 78
    captured = capsys.readouterr()
    assert "runtime_configuration_failed" in captured.err
    assert "secret_file_missing" in captured.err
    assert str(missing_root) not in captured.err
    assert "server-test-password" not in captured.err
    assert "Traceback" not in captured.err


def test_server_unexpected_startup_failure_exits_seventy_safely(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = run_server(environ=LOCAL_ENVIRONMENT, server_factory=ExplodingServerFactory())
    assert result == 70
    captured = capsys.readouterr()
    assert "internal_error" in captured.err
    assert _SENTINEL_HOSTILE_DETAIL not in captured.err
    assert _SENTINEL_HOSTILE_DETAIL not in captured.out
    assert "Traceback" not in captured.err


def test_server_nonzero_system_exit_exits_seventy_safely(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = run_server(environ=LOCAL_ENVIRONMENT, server_factory=ExitingServerFactory(3))
    assert result == 70
    captured = capsys.readouterr()
    assert "internal_error" in captured.err
    assert "Traceback" not in captured.err


def test_server_zero_system_exit_is_clean_shutdown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = run_server(environ=LOCAL_ENVIRONMENT, server_factory=ExitingServerFactory(0))
    assert result == 0
    captured = capsys.readouterr()
    assert "internal_error" not in captured.err
    assert "Traceback" not in captured.err


def test_server_lifespan_configuration_failure_enters_diagnostics_before_framework(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pre-bind keyring refusal is structured before it reaches the server."""
    lifecycle_events: list[str] = []

    class RecordingLifecycle:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def start(self) -> None:
            lifecycle_events.append("start")

        async def stop(self) -> None:
            lifecycle_events.append("stop")

        async def check(self) -> None:
            return None

        async def verify_exclusion_policy_signer(self, *, signing_key_id: str) -> None:
            del signing_key_id
            raise AssertionError("keyring rejection must stop before signer validation")

    async def reject_keyring_coverage(**_: object) -> None:
        raise ConfigurationError(ErrorCode.CONFIGURATION_SECRET_INVALID)

    monkeypatch.setattr("api_runtime.server.DatabaseRuntimeLifecycle", RecordingLifecycle)
    monkeypatch.setattr(
        "api_runtime.server.verify_keyring_covers_required_key_ids",
        reject_keyring_coverage,
    )

    result = run_server(
        environ=LOCAL_ENVIRONMENT,
        server_factory=LifespanExecutingServerFactory(),
    )

    records = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line]
    emergency_records = [
        record
        for record in records
        if record["event"] in {"runtime_configuration_failed", "internal_error"}
    ]
    assert result == 70
    assert emergency_records[0]["event"] == "runtime_configuration_failed"
    assert emergency_records[-1]["event"] == "internal_error"
    assert lifecycle_events == ["start", "stop"]
    assert "Traceback" not in "\n".join(json.dumps(record) for record in emergency_records)
