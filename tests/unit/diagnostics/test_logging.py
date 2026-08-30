"""Structured diagnostics logging: schema, routing, correlation, dependency and fallbacks."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from uuid import UUID

import pytest

import personal_os.diagnostics.logging as diag_logging
from personal_os.diagnostics.context import (
    DiagnosticContext,
    bind_diagnostic_context,
)
from personal_os.diagnostics.events import EventName, SafeToken
from personal_os.diagnostics.logging import (
    DiagnosticLogger,
    configure_diagnostics,
    diagnostic_schema_record,
    reset_diagnostics_for_testing,
)
from personal_os.diagnostics.redaction import (
    fingerprint_text,
    normalize_exception_type,
)
from personal_os.diagnostics.trace_context import resolve_trace_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.models import (
    ConfiguredLogLevel,
    RuntimeEnvironment,
    RuntimeSettings,
    ServiceName,
)

_FIXED_MOMENT = datetime(2026, 8, 13, tzinfo=UTC)
_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{16}")
_OWNED_ATTR = "_diagnostics_owned"


@pytest.fixture
def runtime_settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        service_name=ServiceName.API,
        environment=RuntimeEnvironment.TEST,
        secret_root=tmp_path,
    )


@pytest.fixture(autouse=True)
def _isolated_diagnostics(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Inject a fixed UTC clock and restore root logging state after every case."""
    monkeypatch.setattr(diag_logging, "_current_timestamp", lambda: _FIXED_MOMENT)
    yield
    reset_diagnostics_for_testing()


def _all_lines(stdout: StringIO, stderr: StringIO) -> list[str]:
    return stdout.getvalue().splitlines() + stderr.getvalue().splitlines()


def _capture(
    settings: RuntimeSettings,
) -> tuple[DiagnosticLogger, StringIO, StringIO]:
    stdout = StringIO()
    stderr = StringIO()
    logger = configure_diagnostics(settings, stdout=stdout, stderr=stderr)
    return logger, stdout, stderr


# --- Step 1: schema and stream routing --------------------------------------


def test_emits_one_schema_v1_json_line_to_stdout(
    runtime_settings: RuntimeSettings,
) -> None:
    logger, stdout, stderr = _capture(runtime_settings)

    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": ConfiguredLogLevel.INFO},
    )

    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert records == [
        {
            "diagnostic_schema_version": 1,
            "timestamp": "2026-08-13T00:00:00.000Z",
            "level": "info",
            "service": "api",
            "environment": "test",
            "event": "runtime_configuration_validated",
            "result_code": "succeeded",
            "request_id": None,
            "trace_id": None,
            "configured_log_level": "info",
        }
    ]
    assert stderr.getvalue() == ""


@pytest.mark.parametrize(
    ("stdlib_level", "expects_stderr"),
    [
        (logging.DEBUG, False),
        (logging.INFO, False),
        (logging.WARNING, False),
        (logging.ERROR, True),
        (logging.CRITICAL, True),
    ],
)
def test_routes_each_level_to_exactly_one_stream(
    runtime_settings: RuntimeSettings,
    stdlib_level: int,
    expects_stderr: bool,
) -> None:
    _, stdout, stderr = _capture(runtime_settings)

    logging.getLogger("test.dependency").log(stdlib_level, "dependency event %s", "marker")

    out_lines = stdout.getvalue().splitlines()
    err_lines = stderr.getvalue().splitlines()
    if expects_stderr:
        assert out_lines == []
        assert len(err_lines) == 1
    else:
        assert len(out_lines) == 1
        assert err_lines == []
    assert json.loads((err_lines or out_lines)[0])["event"] == "dependency_log"


# --- Step 2: correlation and idempotency ------------------------------------


def test_bound_context_serializes_canonical_correlation(
    runtime_settings: RuntimeSettings,
) -> None:
    logger, stdout, _ = _capture(runtime_settings)

    request_id = UUID("123e4567-e89b-12d3-a456-426614174000")
    client_request_id = UUID("abcdef12-3456-7890-abcd-ef1234567890")
    trace = resolve_trace_context("00-abcdef1234567890abcdef1234567890-1234567890abcdef-01").context
    workflow_id = SafeToken.parse("ingest-source-commit")
    context = DiagnosticContext(
        request_id=request_id,
        client_request_id=client_request_id,
        trace=trace,
        workflow_id=workflow_id,
    )

    with bind_diagnostic_context(context):
        logger.emit(
            EventName.RUNTIME_CONFIGURATION_VALIDATED,
            {"configured_log_level": ConfiguredLogLevel.INFO},
        )

    record = json.loads(stdout.getvalue().splitlines()[0])
    assert record["request_id"] == str(request_id)
    assert record["trace_id"] == "abcdef1234567890abcdef1234567890"
    assert record["client_request_id"] == str(client_request_id)
    assert record["workflow_id"] == "ingest-source-commit"


def test_unbound_context_omits_optional_correlation(runtime_settings: RuntimeSettings) -> None:
    logger, stdout, _ = _capture(runtime_settings)
    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": ConfiguredLogLevel.INFO},
    )
    record = json.loads(stdout.getvalue().splitlines()[0])
    assert record["request_id"] is None
    assert record["trace_id"] is None
    assert "client_request_id" not in record
    assert "workflow_id" not in record


def test_reconfigure_does_not_multiply_handlers_or_lines(
    runtime_settings: RuntimeSettings,
) -> None:
    stdout = StringIO()
    stderr = StringIO()
    configure_diagnostics(runtime_settings, stdout=stdout, stderr=stderr)
    logger = configure_diagnostics(runtime_settings, stdout=stdout, stderr=stderr)

    root = logging.getLogger()
    assert len(root.handlers) == 2
    assert all(getattr(handler, _OWNED_ATTR, False) for handler in root.handlers)

    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": ConfiguredLogLevel.INFO},
    )
    assert len(_all_lines(stdout, stderr)) == 1


def test_application_event_is_emitted_exactly_once(
    runtime_settings: RuntimeSettings,
) -> None:
    logger, stdout, stderr = _capture(runtime_settings)
    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": ConfiguredLogLevel.INFO},
    )
    assert len(_all_lines(stdout, stderr)) == 1


# --- Step 3: dependency normalization ---------------------------------------


def test_dependency_log_is_fingerprinted_without_message(
    runtime_settings: RuntimeSettings,
) -> None:
    _, stdout, stderr = _capture(runtime_settings)
    sentinel = "do-not-emit-dependency-host"

    logging.getLogger("httpx.transport").info("request to %s failed", sentinel)

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "dependency_log"
    assert record["logger_name"] == "httpx.transport"
    assert record["result_code"] == "degraded"
    assert record["level"] == "info"
    assert _FINGERPRINT_PATTERN.fullmatch(record["message_fingerprint"])
    expected = str(fingerprint_text(f"request to {sentinel} failed"))
    assert record["message_fingerprint"] == expected

    blob = stdout.getvalue() + stderr.getvalue()
    assert sentinel not in blob
    for forbidden in ("message", "args", "exc_info", "traceback"):
        assert forbidden not in record
    assert "/" not in record.get("pathname", "")


def test_diagnostic_schema_record_returns_marked_mapping_or_none() -> None:
    """The public accessor reads exactly the marker attached by ``DiagnosticLogger``."""

    schema: dict[str, object] = {"event": "object_storage_operation_succeeded"}
    marked = logging.LogRecord(
        name="marked",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="",
        args=None,
        exc_info=None,
    )
    setattr(marked, diag_logging._MARKER, schema)
    unmarked = logging.LogRecord(
        name="unmarked",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="",
        args=None,
        exc_info=None,
    )

    assert diagnostic_schema_record(marked) == schema
    assert diagnostic_schema_record(unmarked) is None


def test_dependency_logger_with_invalid_name_falls_back_to_unknown_dependency(
    runtime_settings: RuntimeSettings,
) -> None:
    _, stdout, _ = _capture(runtime_settings)
    logging.getLogger("UPPER/Invalid Name").warning("ignored %s", "x")
    record = json.loads(stdout.getvalue().splitlines()[0])
    assert record["logger_name"] == "unknown.dependency"


def test_dependency_with_hostile_str_emits_rejection_without_raising(
    runtime_settings: RuntimeSettings,
) -> None:
    _, stdout, stderr = _capture(runtime_settings)

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    logging.getLogger("httpx.transport").info("value=%s", Hostile())

    blob = stdout.getvalue() + stderr.getvalue()
    lines = blob.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "logging_payload_rejected"
    assert "boom" not in blob


# --- Step 4: rejection and exception behaviors ------------------------------


def test_unsafe_application_field_emits_only_rejection(
    runtime_settings: RuntimeSettings,
) -> None:
    logger, stdout, stderr = _capture(runtime_settings)
    sentinel = "do-not-emit-unsafe-value"

    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": ConfiguredLogLevel.INFO, "secret_field": sentinel},
    )

    lines = _all_lines(stdout, stderr)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "logging_payload_rejected"
    assert record["reason"] == "unsafe_payload"
    assert record["count"] == 1
    assert "configured_log_level" not in record
    assert sentinel not in stdout.getvalue() + stderr.getvalue()


def test_unknown_application_field_emits_only_rejection(
    runtime_settings: RuntimeSettings,
) -> None:
    logger, stdout, stderr = _capture(runtime_settings)
    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": ConfiguredLogLevel.INFO, "unknown": "x"},
    )
    record = json.loads(_all_lines(stdout, stderr)[0])
    assert record["event"] == "logging_payload_rejected"
    assert record["reason"] == "unsafe_payload"
    assert record["count"] == 1


def test_rejection_counter_hook_called_once_and_swallows_exceptions(
    runtime_settings: RuntimeSettings,
) -> None:
    calls: list[EventName] = []

    def hook(event_name: EventName) -> None:
        calls.append(event_name)
        raise RuntimeError("hook boom")

    stdout = StringIO()
    stderr = StringIO()
    logger = configure_diagnostics(
        runtime_settings,
        stdout=stdout,
        stderr=stderr,
        rejection_counter_hook=hook,
    )

    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": ConfiguredLogLevel.INFO, "unknown": "x"},
    )

    assert calls == [EventName.RUNTIME_CONFIGURATION_VALIDATED]
    lines = _all_lines(stdout, stderr)
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "logging_payload_rejected"


def test_emit_application_error_emits_registered_fields_without_traceback(
    runtime_settings: RuntimeSettings,
) -> None:
    logger, _, stderr = _capture(runtime_settings)

    try:
        raise ValueError("do-not-emit-cause-message")
    except ValueError as cause:
        try:
            raise ConfigurationError(
                ErrorCode.CONFIGURATION_INVALID,
                safe_details={
                    "count": 2,
                    "field_names": (SafeToken.parse("log_level"),),
                },
            ) from cause
        except ConfigurationError as error:
            captured = error

    logger.emit_application_error(captured)

    lines = stderr.getvalue().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "runtime_configuration_failed"
    assert record["error_code"] == "configuration_invalid"
    assert record["error_category"] == "configuration"
    assert record["is_retryable"] is False
    assert record["count"] == 2
    assert "field_names" not in record
    assert "message" not in record
    assert "args" not in record
    blob = stderr.getvalue()
    assert "do-not-emit-cause-message" not in blob
    assert "traceback" not in blob.lower()


def test_emit_internal_error_emits_normalized_type_and_stack_fingerprint(
    runtime_settings: RuntimeSettings,
) -> None:
    logger, _, stderr = _capture(runtime_settings)

    try:
        raise RuntimeError("do-not-emit-internal-message")
    except RuntimeError as exception:
        captured = exception

    logger.emit_internal_error(captured)

    lines = stderr.getvalue().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "internal_error"
    assert record["error_code"] == "internal_error"
    assert record["error_category"] == "internal"
    assert record["is_retryable"] is False
    assert record["exception_type"] == str(normalize_exception_type(captured))
    assert _FINGERPRINT_PATTERN.fullmatch(record["stack_fingerprint"])
    for forbidden in ("message", "args", "exc_info"):
        assert forbidden not in record
    blob = stderr.getvalue()
    assert "do-not-emit-internal-message" not in blob


def test_serializer_failure_emits_one_constant_rejection_line(
    runtime_settings: RuntimeSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger, stdout, stderr = _capture(runtime_settings)

    def boom(_record: object) -> str:
        raise ValueError("serialize fail")

    monkeypatch.setattr(diag_logging, "_serialize", boom)

    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": ConfiguredLogLevel.INFO},
    )

    lines = _all_lines(stdout, stderr)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "logging_payload_rejected"
    assert record["reason"] == "unsafe_payload"


def test_fallback_failure_does_not_raise_or_recurse(
    runtime_settings: RuntimeSettings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_calls = 0

    class BrokenStream(StringIO):
        def write(self, value: str) -> int:
            nonlocal write_calls
            write_calls += 1
            raise OSError("broken stream")

    stdout = BrokenStream()
    stderr = BrokenStream()
    logger = configure_diagnostics(runtime_settings, stdout=stdout, stderr=stderr)

    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": ConfiguredLogLevel.INFO},
    )

    # No exception escaped and the write path did not spin into a loop.
    assert write_calls <= 4
    captured = capsys.readouterr()
    total_emergency = captured.err.count("logging_payload_rejected")
    assert total_emergency <= 1


def test_reset_diagnostics_for_testing_restores_root_state(
    runtime_settings: RuntimeSettings,
) -> None:
    root = logging.getLogger()
    prior_handlers = list(root.handlers)
    prior_level = root.level

    configure_diagnostics(runtime_settings)
    assert any(getattr(h, _OWNED_ATTR, False) for h in root.handlers)

    reset_diagnostics_for_testing()
    assert root.handlers == prior_handlers
    assert root.level == prior_level
