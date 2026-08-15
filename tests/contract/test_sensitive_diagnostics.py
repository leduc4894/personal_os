"""Cross-layer leak corpus: distinct sentinels must never reach any diagnostic sink.

Every scenario feeds a unique ``do-not-emit-*`` sentinel through one boundary of
the diagnostics surface (settings, correlation context, dependency logging, error
causes, hostile objects) and asserts the sentinel never appears in captured
stdout, stderr, serialized return values, ``str(error)`` or ``repr(error)``. No
test in this module is conditionally skipped.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from api_runtime.request_context import RequestContextMiddleware

from personal_os.diagnostics.context import (
    DiagnosticContext,
    bind_diagnostic_context,
    create_diagnostic_context,
)
from personal_os.diagnostics.events import EventName, ObjectDigestPrefix, SafeToken
from personal_os.diagnostics.logging import (
    DiagnosticLogger,
    configure_diagnostics,
    emit_emergency_application_error,
    emit_emergency_internal_error,
    reset_diagnostics_for_testing,
)
from personal_os.diagnostics.trace_context import resolve_trace_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError, SecretFileError
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.runtime_configuration.loading import load_runtime_settings
from personal_os.runtime_configuration.models import (
    ConfiguredLogLevel,
    RuntimeSettings,
    ServiceName,
)

_SENTINELS = (
    "do-not-emit-secret-value",
    "do-not-emit-secret-filename",
    "do-not-emit-resolved-root",
    "do-not-emit-sibling-root",
    "do-not-emit-client-request-id",
    "do-not-emit-traceparent",
    "do-not-emit-dependency-message",
    "do-not-emit-dependency-argument",
    "do-not-emit-cause-message",
    "do-not-emit-exception-message",
    "do-not-emit-exception-arg",
    "do-not-emit-forbidden-field-value",
    "do-not-emit-sensitive-pattern",
    "do-not-emit-unknown-setting-name",
    "do-not-emit-object-full-digest",
    "do-not-emit-object-key",
    "do-not-emit-bucket-name",
    "do-not-emit-r2-endpoint",
    "do-not-emit-spool-filename",
    "do-not-emit-response-body",
    "do-not-emit-request-header",
    "do-not-emit-provider-request-id",
    "do-not-emit-provider-exception",
    "do-not-emit-source-path",
    "do-not-emit-media-type",
    "do-not-emit-raw-path",
    "do-not-emit-query-value",
)

_FORBIDDEN_FIELD_NAMES = (
    "password",
    "token",
    "secret",
    "authorization",
    "credential",
    "cookie",
    "content",
    "body",
    "query",
    "vector",
    "embedding",
    "traceback",
)

_SENSITIVE_VALUE_PATTERNS = (
    "Bearer do-not-emit-sensitive-pattern",
    "eyJdo-not-emit-sensitive-pattern.aaa.bbb",
    "-----BEGIN PRIVATE KEY-----do-not-emit-sensitive-pattern",
    "https://user:do-not-emit-sensitive-pattern@host",
    "x-amz-signature=do-not-emit-sensitive-pattern",
)


@pytest.fixture
def runtime_settings(tmp_path: Path) -> RuntimeSettings:
    return load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_ENVIRONMENT": "test",
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
        },
    )


@pytest.fixture(autouse=True)
def _reset_diagnostics_after_each() -> Iterator[None]:
    yield
    reset_diagnostics_for_testing()


def _configure(settings: RuntimeSettings) -> tuple[DiagnosticLogger, StringIO, StringIO]:
    stdout = StringIO()
    stderr = StringIO()
    logger = configure_diagnostics(settings, stdout=stdout, stderr=stderr)
    return logger, stdout, stderr


def _blob(*streams: StringIO) -> str:
    return "".join(stream.getvalue() for stream in streams)


def _assert_no_sentinel(*blobs: str, sentinels: tuple[str, ...] = _SENTINELS) -> None:
    combined = "\n".join(blobs)
    for sentinel in sentinels:
        assert sentinel not in combined, f"sentinel leaked: {sentinel}"


def _all_records_parsable(*streams: StringIO) -> None:
    for stream in streams:
        for line in stream.getvalue().splitlines():
            assert isinstance(json.loads(line), dict)


# --- settings surface -------------------------------------------------------


def test_invalid_settings_value_sentinel_does_not_leak(
    runtime_settings: RuntimeSettings,
) -> None:
    sentinel = "do-not-emit-unknown-setting-name"
    error: ConfigurationError | None = None
    try:
        load_runtime_settings(
            ServiceName.API,
            environ={
                "KNOWLEDGE_ENVIRONMENT": sentinel,
                "KNOWLEDGE_SECRET_ROOT": str(runtime_settings.secret_root),
            },
        )
    except ConfigurationError as raised:
        error = raised

    assert error is not None
    logger, stdout, stderr = _configure(runtime_settings)
    logger.emit_application_error(error)
    _all_records_parsable(stdout, stderr)
    _assert_no_sentinel(_blob(stdout, stderr), str(error), repr(error), sentinels=(sentinel,))


def test_secret_filename_and_value_never_serialized(
    runtime_settings: RuntimeSettings,
) -> None:
    filename_sentinel = "do-not-emit-secret-filename"
    value_sentinel = "do-not-emit-secret-value"
    error = SecretFileError(
        ErrorCode.SECRET_FILE_MISSING,
        safe_details={"reason": SafeToken.parse("secret_file_missing")},
    )
    try:
        raise RuntimeError(f"{filename_sentinel}={value_sentinel}") from None
    except RuntimeError as cause:
        try:
            raise error from cause
        except SecretFileError as captured:
            error = captured

    logger, stdout, stderr = _configure(runtime_settings)
    logger.emit_application_error(error)
    _all_records_parsable(stdout, stderr)
    _assert_no_sentinel(
        _blob(stdout, stderr),
        str(error),
        repr(error),
        sentinels=(filename_sentinel, value_sentinel),
    )


# --- correlation context surface --------------------------------------------


def test_rejected_client_request_id_and_traceparent_sentinels_never_leak(
    runtime_settings: RuntimeSettings,
) -> None:
    client_sentinel = "do-not-emit-client-request-id"
    trace_sentinel = "do-not-emit-traceparent"
    resolved = create_diagnostic_context(
        client_request_id=client_sentinel,
        traceparent=trace_sentinel,
    )
    logger, stdout, stderr = _configure(runtime_settings)

    with bind_diagnostic_context(resolved.context):
        logger.emit(
            EventName.RUNTIME_CONFIGURATION_VALIDATED,
            {"configured_log_level": ConfiguredLogLevel.INFO},
        )

    _all_records_parsable(stdout, stderr)
    _assert_no_sentinel(
        _blob(stdout, stderr),
        str(resolved),
        repr(resolved),
        repr(resolved.context),
        sentinels=(client_sentinel, trace_sentinel),
    )


# --- api request observation surface -----------------------------------------


@pytest.mark.asyncio
async def test_api_request_observation_sentinels_never_leak(
    runtime_settings: RuntimeSettings,
) -> None:
    """Raw ASGI requests with hostile paths, queries, headers and correlation ids.

    The middleware owns the request id and trace context; the access events and
    the rejection reasons it feeds the diagnostic logger may only ever carry
    closed enum values, status and duration. The raw path, query string, custom
    header values and the malformed correlation inputs must never survive into
    any diagnostic sink.
    """
    logger, stdout, stderr = _configure(runtime_settings)

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/do-not-emit-raw-path",
        "raw_path": b"/do-not-emit-raw-path",
        "query_string": b"q=do-not-emit-query-value",
        "root_path": "",
        "headers": [
            (b"x-client-request-id", b"do-not-emit-client-request-id"),
            (b"traceparent", b"do-not-emit-traceparent"),
            (b"x-custom-header", b"do-not-emit-request-header"),
        ],
        "client": ("127.0.0.1", 42000),
        "server": ("127.0.0.1", 80),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    started: dict[str, Any] | None = None

    async def send(message: dict[str, Any]) -> None:
        nonlocal started
        if message["type"] == "http.response.start":
            started = message

    await RequestContextMiddleware(app, event_sink=logger)(scope, receive, send)

    assert started is not None
    response_headers = {name: value for name, value in started["headers"]}
    assert b"traceparent" in response_headers
    assert b"x-request-id" in response_headers

    _all_records_parsable(stdout, stderr)
    _assert_no_sentinel(_blob(stdout, stderr))

    records = [json.loads(line) for line in _blob(stdout, stderr).splitlines()]
    base_keys = {
        "diagnostic_schema_version",
        "timestamp",
        "service",
        "environment",
        "request_id",
        "trace_id",
        "level",
        "event",
        "result_code",
    }
    events = [record["event"] for record in records]
    assert "api_request_completed" in events
    assert "client_request_id_rejected" in events
    assert "trace_context_replaced" in events
    closed_access_fields = {"http_method", "route", "status_code", "duration_ms"}
    for record in records:
        extra_fields = set(record) - base_keys
        if record["event"] == "api_request_completed":
            assert extra_fields == closed_access_fields
            assert record["route"] in {
                "/api/health/live",
                "/api/health/ready",
                "/api/openapi.json",
                "unmatched",
            }
            assert record["http_method"] in {"GET", "OTHER"}
        else:
            assert extra_fields == {"reason"}
            assert record["reason"] == "invalid_format"


# --- forbidden field families and sensitive patterns ------------------------


@pytest.mark.parametrize("field_name", _FORBIDDEN_FIELD_NAMES)
def test_forbidden_field_family_rejected_without_leak(
    runtime_settings: RuntimeSettings, field_name: str
) -> None:
    sentinel = "do-not-emit-forbidden-field-value"
    logger, stdout, stderr = _configure(runtime_settings)
    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": ConfiguredLogLevel.INFO, field_name: sentinel},
    )
    _all_records_parsable(stdout, stderr)
    _assert_no_sentinel(_blob(stdout, stderr), sentinels=(sentinel,))
    record = json.loads(_all_records_one(stdout, stderr))
    assert record["event"] == "logging_payload_rejected"


@pytest.mark.parametrize("value", _SENSITIVE_VALUE_PATTERNS)
def test_sensitive_value_pattern_rejected_without_leak(
    runtime_settings: RuntimeSettings, value: str
) -> None:
    logger, stdout, stderr = _configure(runtime_settings)
    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": value},
    )
    _all_records_parsable(stdout, stderr)
    _assert_no_sentinel(_blob(stdout, stderr), sentinels=("do-not-emit-sensitive-pattern",))


def _all_records_one(stdout: StringIO, stderr: StringIO) -> str:
    lines = stdout.getvalue().splitlines() + stderr.getvalue().splitlines()
    assert len(lines) == 1
    return lines[0]


# --- dependency logging surface ---------------------------------------------


def test_dependency_message_and_argument_sentinels_never_leak(
    runtime_settings: RuntimeSettings,
) -> None:
    message_sentinel = "do-not-emit-dependency-message"
    argument_sentinel = "do-not-emit-dependency-argument"
    _, stdout, stderr = _configure(runtime_settings)
    logging.getLogger("httpx.transport").warning(
        "%s could not reach %s", message_sentinel, argument_sentinel
    )
    _all_records_parsable(stdout, stderr)
    _assert_no_sentinel(
        _blob(stdout, stderr),
        sentinels=(message_sentinel, argument_sentinel),
    )
    record = json.loads(stdout.getvalue().splitlines()[0])
    assert record["event"] == "dependency_log"
    assert "message" not in record
    assert "args" not in record


# --- expected error causes and unexpected exceptions ------------------------


def test_expected_error_cause_and_unexpected_exception_sentinels_never_leak(
    runtime_settings: RuntimeSettings,
) -> None:
    cause_sentinel = "do-not-emit-cause-message"
    exception_sentinel = "do-not-emit-exception-message"
    argument_sentinel = "do-not-emit-exception-arg"

    try:
        raise ValueError(cause_sentinel)
    except ValueError as cause:
        try:
            raise ConfigurationError(
                ErrorCode.CONFIGURATION_INVALID, safe_details={"count": 1}
            ) from cause
        except ConfigurationError as application_error:
            captured_application = application_error

    try:
        raise RuntimeError(exception_sentinel, argument_sentinel)
    except RuntimeError as internal:
        captured_internal = internal

    # The source objects carry the sentinels (otherwise the boundary test is moot).
    assert cause_sentinel in str(captured_application.__cause__)
    assert exception_sentinel in str(captured_internal)
    assert argument_sentinel in repr(captured_internal)

    logger, stdout, stderr = _configure(runtime_settings)
    logger.emit_application_error(captured_application)
    logger.emit_internal_error(captured_internal)

    _all_records_parsable(stdout, stderr)
    # Only the diagnostic sinks are scanned: str/repr of a raw exception are the
    # source, never a sink. ApplicationError str/repr never expose the cause, so
    # they are scanned as an additional contract check.
    _assert_no_sentinel(
        _blob(stdout, stderr),
        str(captured_application),
        repr(captured_application),
        sentinels=(cause_sentinel, exception_sentinel, argument_sentinel),
    )


# --- object-storage provider boundary ---------------------------------------


_OBJECT_STORAGE_SENTINELS = (
    "do-not-emit-object-full-digest",
    "do-not-emit-object-key",
    "do-not-emit-bucket-name",
    "do-not-emit-r2-endpoint",
    "do-not-emit-spool-filename",
    "do-not-emit-response-body",
    "do-not-emit-request-header",
    "do-not-emit-provider-request-id",
    "do-not-emit-provider-exception",
    "do-not-emit-source-path",
    "do-not-emit-media-type",
)


def test_object_storage_provider_exception_and_event_fields_never_leak(
    runtime_settings: RuntimeSettings,
) -> None:
    # A real provider failure carries bucket, endpoint, full digest, key,
    # filename, body, header, request id and exception text. None of these may
    # survive into the typed error, its serialized form or any diagnostic sink.
    provider_exception = RuntimeError(
        "do-not-emit-provider-exception "
        "bucket=do-not-emit-bucket-name "
        "endpoint=do-not-emit-r2-endpoint "
        "body=do-not-emit-response-body "
        "header=do-not-emit-request-header "
        "request_id=do-not-emit-provider-request-id "
        "key=do-not-emit-object-key "
        "digest=do-not-emit-object-full-digest "
        "path=do-not-emit-source-path "
        "media=do-not-emit-media-type "
        "spool=do-not-emit-spool-filename"
    )
    try:
        raise provider_exception
    except RuntimeError as cause:
        try:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_UNAVAILABLE) from cause
        except ObjectStorageError as error:
            captured = error

    # The source exception carries every sentinel; otherwise the boundary test is moot.
    assert "do-not-emit-bucket-name" in str(captured.__cause__)

    logger, stdout, stderr = _configure(runtime_settings)
    logger.emit_application_error(captured)

    # Object-storage events accept only the registered safe fields; the digest
    # prefix is bounded to 12 lowercase hex characters and never the full digest.
    logger.emit(
        EventName.OBJECT_STORAGE_OPERATION_SUCCEEDED,
        {
            "operation": SafeToken.parse("store_stream"),
            "duration_ms": 42,
            "size_bytes": 1024,
            "attempt_count": 1,
            "provider": SafeToken.parse("r2"),
        },
    )
    logger.emit(
        EventName.OBJECT_STORAGE_OPERATION_FAILED,
        {
            "operation": SafeToken.parse("store_stream"),
            "duration_ms": 99,
            "attempt_count": 2,
            "provider": SafeToken.parse("r2"),
            "error_code": SafeToken.parse("object_storage_unavailable"),
            "error_category": SafeToken.parse("dependency"),
            "is_retryable": True,
            "object_digest_prefix": ObjectDigestPrefix.parse("0123456789ab"),
        },
    )

    _all_records_parsable(stdout, stderr)
    _assert_no_sentinel(
        _blob(stdout, stderr),
        str(captured),
        repr(captured),
        sentinels=_OBJECT_STORAGE_SENTINELS,
    )
    # The registered digest prefix is emitted; the full digest sentinel is not.
    blob = _blob(stdout, stderr)
    assert "0123456789ab" in blob
    assert "do-not-emit-object-full-digest" not in blob


# --- hostile objects --------------------------------------------------------


def test_hostile_object_values_are_rejected_safely(
    runtime_settings: RuntimeSettings,
) -> None:
    class HostileValue:
        def __str__(self) -> str:
            raise RuntimeError("do-not-emit-exception-message")

        def __repr__(self) -> str:
            raise RuntimeError("do-not-emit-exception-message")

    hostile = HostileValue()
    logger, stdout, stderr = _configure(runtime_settings)
    logger.emit(
        EventName.RUNTIME_CONFIGURATION_VALIDATED,
        {"configured_log_level": ConfiguredLogLevel.INFO, "hostile": hostile},
    )
    logging.getLogger("httpx.transport").info("value=%s", hostile)
    _all_records_parsable(stdout, stderr)
    _assert_no_sentinel(_blob(stdout, stderr), sentinels=("do-not-emit-exception-message",))


def test_hostile_dependency_argument_does_not_raise(
    runtime_settings: RuntimeSettings,
) -> None:
    class HostileArg:
        def __str__(self) -> str:
            raise RuntimeError("do-not-emit-dependency-argument")

    _, stdout, stderr = _configure(runtime_settings)
    logging.getLogger("httpx.transport").info("calling %s", HostileArg())
    _all_records_parsable(stdout, stderr)
    _assert_no_sentinel(_blob(stdout, stderr), sentinels=("do-not-emit-dependency-argument",))


# --- emergency helpers never leak -------------------------------------------


def test_emergency_helpers_never_leak_context_or_exception_text(
    runtime_settings: RuntimeSettings,
) -> None:
    emergency_stderr = StringIO()
    context = DiagnosticContext(
        request_id=UUID("123e4567-e89b-12d3-a456-426614174000"),
        client_request_id=None,
        trace=resolve_trace_context(
            "00-abcdef1234567890abcdef1234567890-1234567890abcdef-01"
        ).context,
    )
    cause_sentinel = "do-not-emit-cause-message"
    internal_sentinel = "do-not-emit-exception-message"

    try:
        raise ValueError(cause_sentinel)
    except ValueError as cause:
        try:
            raise ConfigurationError(
                ErrorCode.CONFIGURATION_INVALID, safe_details={"count": 1}
            ) from cause
        except ConfigurationError as application_error:
            captured_application = application_error

    try:
        raise RuntimeError(internal_sentinel)
    except RuntimeError as internal:
        captured_internal = internal

    _configure(runtime_settings)
    emit_emergency_application_error(
        ServiceName.API, context, captured_application, stderr=emergency_stderr
    )
    emit_emergency_internal_error(
        ServiceName.API, context, captured_internal, stderr=emergency_stderr
    )
    _all_records_parsable(emergency_stderr)
    _assert_no_sentinel(
        emergency_stderr.getvalue(),
        sentinels=(cause_sentinel, internal_sentinel),
    )


# --- collection guard -------------------------------------------------------


def test_file_collects_sentinel_scanning_tests() -> None:
    """The leak corpus must collect tests when the file runs directly."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__)), "--collect-only", "-q"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    collected = [line for line in result.stdout.splitlines() if "::" in line]
    assert len(collected) >= 10
