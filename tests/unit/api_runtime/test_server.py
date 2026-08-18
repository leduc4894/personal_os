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
