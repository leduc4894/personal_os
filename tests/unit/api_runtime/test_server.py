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
from fastapi import FastAPI

# One hermetic secret root backing the local environment snapshot, so the
# password read inside run_server succeeds without touching process secrets.
_SECRET_DIRECTORY = tempfile.TemporaryDirectory(prefix="api-runtime-server-secrets-")
atexit.register(_SECRET_DIRECTORY.cleanup)
_SECRET_ROOT = Path(_SECRET_DIRECTORY.name)
(_SECRET_ROOT / "postgres_application_password").write_text(
    "server-test-password\n", encoding="utf-8"
)

LOCAL_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        "KNOWLEDGE_ENVIRONMENT": "local",
        "KNOWLEDGE_SECRET_ROOT": str(_SECRET_ROOT),
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

    def run(self) -> None:
        """Simulate an immediately clean shutdown without binding a socket."""
        return None


class RecordingServerFactory:
    """Factory capturing the single Uvicorn config the runner prepared."""

    def __init__(self) -> None:
        self.config: uvicorn.Config | None = None

    def __call__(self, config: uvicorn.Config) -> RecordingServer:
        assert self.config is None, "run_server must prepare exactly one server"
        self.config = config
        return RecordingServer(config)


class ExplodingServerFactory:
    """Factory failing startup after configuration, with hostile detail text."""

    def __call__(self, config: uvicorn.Config) -> RecordingServer:
        del config
        raise RuntimeError(_SENTINEL_HOSTILE_DETAIL)


class ExitingServer(RecordingServer):
    """Server whose run raises ``SystemExit`` like Uvicorn on bind failure."""

    def __init__(self, exit_code: int | None, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.exit_code = exit_code

    def run(self) -> None:
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


def test_server_builds_local_application_with_openapi_route() -> None:
    captured = RecordingServerFactory()
    assert run_server(environ=LOCAL_ENVIRONMENT, server_factory=captured) == 0
    application = captured.config.app
    assert isinstance(application, FastAPI)
    paths = {route.path for route in application.routes}
    assert "/api/health/live" in paths
    assert "/api/health/ready" in paths
    assert "/api/openapi.json" in paths


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
