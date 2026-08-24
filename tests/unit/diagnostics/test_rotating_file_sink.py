"""Rotating local diagnostics file sink: activation, levels, rotation bounds, fail-closed."""

from __future__ import annotations

import json
import logging
import logging.handlers
from collections.abc import Iterator
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest

import personal_os.diagnostics.logging as diag_logging
from personal_os.diagnostics.events import EventName
from personal_os.diagnostics.logging import (
    DiagnosticLogger,
    configure_diagnostics,
    reset_diagnostics_for_testing,
)
from personal_os.runtime_configuration.models import (
    ConfiguredLogLevel,
    RuntimeEnvironment,
    RuntimeSettings,
    ServiceName,
)

_FIXED_MOMENT = datetime(2026, 8, 13, tzinfo=UTC)
_OWNED_ATTR = "_diagnostics_owned"
_LOG_FILE_NAME = "api-diagnostics.log"


@pytest.fixture(autouse=True)
def _isolated_diagnostics(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Inject a fixed UTC clock and restore root logging state after every case."""
    monkeypatch.setattr(diag_logging, "_current_timestamp", lambda: _FIXED_MOMENT)
    yield
    reset_diagnostics_for_testing()


def _settings(tmp_path: Path, log_dir: Path | None) -> RuntimeSettings:
    return RuntimeSettings(
        service_name=ServiceName.API,
        environment=RuntimeEnvironment.TEST,
        secret_root=tmp_path,
        diagnostics_log_dir=log_dir,
    )


def _owned_handlers() -> list[logging.Handler]:
    root = logging.getLogger()
    return [handler for handler in root.handlers if getattr(handler, _OWNED_ATTR, False)]


def _capture(
    settings: RuntimeSettings,
    *,
    file_rotation_max_bytes: int | None = None,
    file_rotation_backup_count: int | None = None,
) -> tuple[DiagnosticLogger, StringIO, StringIO]:
    stdout = StringIO()
    stderr = StringIO()
    rotation_kwargs: dict[str, int] = {}
    if file_rotation_max_bytes is not None:
        rotation_kwargs["file_rotation_max_bytes"] = file_rotation_max_bytes
    if file_rotation_backup_count is not None:
        rotation_kwargs["file_rotation_backup_count"] = file_rotation_backup_count
    logger = configure_diagnostics(settings, stdout=stdout, stderr=stderr, **rotation_kwargs)
    return logger, stdout, stderr


def _emit_validated(logger: DiagnosticLogger) -> None:
    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": ConfiguredLogLevel.INFO},
    )


# --- Activation: disabled by default ----------------------------------------


def test_without_log_dir_keeps_exactly_the_two_stream_handlers(tmp_path: Path) -> None:
    _capture(_settings(tmp_path, log_dir=None))

    handlers = _owned_handlers()
    assert len(handlers) == 2
    assert all(
        not isinstance(handler, logging.handlers.RotatingFileHandler) for handler in handlers
    )


# --- Activation: enabled writes the same JSON lines to file and stdout -------


def test_log_dir_writes_the_same_redacted_json_lines_to_file_and_stdout(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "runtime-logs"
    logger, stdout, _ = _capture(_settings(tmp_path, log_dir=log_dir))

    _emit_validated(logger)

    assert len(_owned_handlers()) == 3
    assert all(getattr(handler, _OWNED_ATTR, False) for handler in logging.getLogger().handlers)
    file_lines = (log_dir / _LOG_FILE_NAME).read_text(encoding="utf-8").splitlines()
    stdout_lines = stdout.getvalue().splitlines()
    assert len(stdout_lines) == 1
    assert len(file_lines) == 1
    assert json.loads(file_lines[0]) == json.loads(stdout_lines[0])
    record = json.loads(file_lines[0])
    assert record["event"] == "runtime_configuration_validated"
    assert record["service"] == "api"
    assert record["environment"] == "test"


def test_enabled_sink_receives_every_level_line(tmp_path: Path) -> None:
    log_dir = tmp_path / "runtime-logs"
    logger, stdout, stderr = _capture(_settings(tmp_path, log_dir=log_dir))

    _emit_validated(logger)
    logger.emit_internal_error(RuntimeError("do-not-emit-internal-message"))

    file_lines = (log_dir / _LOG_FILE_NAME).read_text(encoding="utf-8").splitlines()
    stdout_lines = stdout.getvalue().splitlines()
    stderr_lines = stderr.getvalue().splitlines()
    assert len(stdout_lines) == 1
    assert len(stderr_lines) == 1
    assert len(file_lines) == 2
    events = [json.loads(line)["event"] for line in file_lines]
    assert events == ["runtime_configuration_validated", "internal_error"]
    assert stdout_lines[0] in file_lines
    assert stderr_lines[0] in file_lines
    assert "do-not-emit-internal-message" not in (log_dir / _LOG_FILE_NAME).read_text(
        encoding="utf-8"
    )


# --- Rotation bounds file count and per-file size ----------------------------


def test_rotation_bounds_file_count_and_each_file_size(tmp_path: Path) -> None:
    log_dir = tmp_path / "runtime-logs"
    settings = _settings(tmp_path, log_dir=log_dir)

    logger, _, _ = _capture(settings)
    _emit_validated(logger)
    first_line_bytes = (log_dir / _LOG_FILE_NAME).stat().st_size
    assert first_line_bytes > 0

    # Two and a half lines per rotated file: two lines always fit, the third rolls.
    max_bytes = 2 * first_line_bytes + first_line_bytes // 2
    logger, _, _ = _capture(
        settings,
        file_rotation_max_bytes=max_bytes,
        file_rotation_backup_count=1,
    )
    for _ in range(8):
        _emit_validated(logger)

    rotated_files = sorted(path.name for path in log_dir.iterdir())
    assert rotated_files == [f"{_LOG_FILE_NAME}", f"{_LOG_FILE_NAME}.1"]
    total_lines = 0
    for name in rotated_files:
        path = log_dir / name
        assert path.stat().st_size <= max_bytes
        lines = path.read_text(encoding="utf-8").splitlines()
        assert all(json.loads(line)["event"] == "runtime_configuration_validated" for line in lines)
        total_lines += len(lines)
    assert 0 < total_lines < 9


# --- Fail-closed activation --------------------------------------------------


def test_unwritable_log_dir_fails_closed_with_one_rejection_line(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    settings = _settings(tmp_path, log_dir=blocker / "runtime-logs")

    logger, stdout, stderr = _capture(settings)

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "logging_payload_rejected"
    assert record["result_code"] == "rejected"
    assert record["reason"] == "diagnostics_log_dir_unavailable"
    assert record["count"] == 1
    assert stderr.getvalue() == ""
    assert len(_owned_handlers()) == 2

    _emit_validated(logger)
    resumed = stdout.getvalue().splitlines()
    assert len(resumed) == 2
    assert json.loads(resumed[1])["event"] == "runtime_configuration_validated"


def test_relative_log_dir_fails_closed_without_creating_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path, log_dir=Path("relative-runtime-logs"))

    _logger, stdout, stderr = _capture(settings)

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "logging_payload_rejected"
    assert record["reason"] == "diagnostics_log_dir_unavailable"
    assert stderr.getvalue() == ""
    assert len(_owned_handlers()) == 2
    assert not (tmp_path / "relative-runtime-logs").exists()
