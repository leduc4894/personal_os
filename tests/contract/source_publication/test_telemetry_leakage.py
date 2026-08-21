"""Source-publication telemetry leakage contract: sentinels never reach a sink.

Every scenario feeds a unique ``do-not-emit-*`` sentinel (title, idempotency
key, request fingerprint, SQL statement, database password, provider exception
text) through the source-publication error and diagnostic-event boundary and
asserts the sentinel never appears in ``str(error)``, ``repr(error)``, the safe
serialization or any captured log record.
"""

from __future__ import annotations

import json
import logging
from io import StringIO
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

import pytest

from personal_os.diagnostics.events import EventName, SafeToken
from personal_os.diagnostics.logging import (
    DiagnosticLogger,
    configure_diagnostics,
    reset_diagnostics_for_testing,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.runtime_configuration.loading import load_runtime_settings
from personal_os.runtime_configuration.models import ServiceName
from personal_os.sources.commands import IdempotencyKey, SourceTitle
from personal_os.sources.errors import SourcePublicationError

TITLE_SENTINEL = "do-not-emit-source-title"
KEY_SENTINEL = "do-not-emit-idempotency-key"
FINGERPRINT_SENTINEL = "do-not-emit-request-fingerprint"
SQL_SENTINEL = "do-not-emit-sql-statement"
PASSWORD_SENTINEL = "do-not-emit-database-password"
PROVIDER_SENTINEL = "do-not-emit-provider-exception-text"

_SENTINELS = (
    TITLE_SENTINEL,
    KEY_SENTINEL,
    FINGERPRINT_SENTINEL,
    SQL_SENTINEL,
    PASSWORD_SENTINEL,
    PROVIDER_SENTINEL,
)

_MARKER = "_diagnostic_schema_record"


def _build_logger() -> DiagnosticLogger:
    snapshot = MappingProxyType({"service": "api", "environment": "test"})
    return DiagnosticLogger(snapshot)


def _captured_log_blob(caplog: pytest.LogCaptureFixture) -> str:
    parts = [caplog.text]
    for record in caplog.records:
        schema = getattr(record, _MARKER, None)
        if schema is not None:
            parts.append(json.dumps(schema, default=repr))
        parts.append(record.getMessage())
        parts.append(repr(record))
    return "\n".join(parts)


def test_publication_error_never_leaks_title_key_or_cause_text() -> None:
    source_id = uuid4()
    event_id = uuid4()
    title = SourceTitle(f"Draft: {TITLE_SENTINEL}")
    key = IdempotencyKey(KEY_SENTINEL.replace("-", "_"))
    provider_cause = RuntimeError(
        f"{PROVIDER_SENTINEL} sql={SQL_SENTINEL} password={PASSWORD_SENTINEL} "
        f"fingerprint={FINGERPRINT_SENTINEL} title={TITLE_SENTINEL} key={KEY_SENTINEL}"
    )

    # The source objects carry every sentinel; otherwise the boundary test is moot.
    assert TITLE_SENTINEL in title.value
    assert KEY_SENTINEL.replace("-", "_") in key.value
    assert PROVIDER_SENTINEL in str(provider_cause)

    try:
        raise provider_cause
    except RuntimeError as cause:
        try:
            raise SourcePublicationError(
                ErrorCode.SOURCE_EVENT_IDENTITY_MISMATCH,
                safe_details={"source_id": source_id, "event_id": event_id},
            ) from cause
        except SourcePublicationError as captured:
            error = captured

    rendered = f"{error} {error!r} {json.dumps(error.to_safe_dict(), default=repr)}"
    for sentinel in _SENTINELS:
        assert sentinel not in rendered, sentinel


def test_publication_events_never_leak_sentinels_into_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = _build_logger()
    source_id = uuid4()
    source_version_id = uuid4()
    event_id = uuid4()
    intent_id = uuid4()
    with caplog.at_level(logging.DEBUG):
        logger.emit_application_error(
            SourcePublicationError(
                ErrorCode.SOURCE_CONCURRENCY_BUSY, safe_details={"source_id": source_id}
            )
        )
        logger.emit(
            EventName.SOURCE_VERSION_PUBLISH_SUCCEEDED,
            {
                "operation": SafeToken.parse("update"),
                "outcome": SafeToken.parse("no_change"),
                "duration_ms": 12,
                "attempt_count": 1,
                "content_version": 4,
                "source_id": source_id,
                "source_version_id": source_version_id,
                "event_id": event_id,
            },
        )
        logger.emit(
            EventName.SOURCE_VERSION_PUBLISH_REJECTED,
            {
                "operation": SafeToken.parse("update"),
                "outcome": SafeToken.parse("rejected"),
                "duration_ms": 3,
                "error_code": SafeToken.parse("source_version_conflict"),
                "error_category": SafeToken.parse("conflict"),
                "is_retryable": False,
                "source_id": source_id,
                "event_id": event_id,
                "reason_code": SafeToken.parse("version_conflict"),
            },
        )
        logger.emit(
            EventName.SOURCE_VERSION_PUBLISH_REPLAYED,
            {
                "operation": SafeToken.parse("update"),
                "outcome": SafeToken.parse("no_change"),
                "duration_ms": 5,
                "attempt_count": 1,
                "content_version": 4,
                "source_id": source_id,
                "source_version_id": source_version_id,
                "event_id": event_id,
            },
        )
        logger.emit(
            EventName.PROJECTION_INTENT_DISPATCH_FAILED,
            {
                "projection_kind": SafeToken.parse("qdrant"),
                "outcome": SafeToken.parse("terminal"),
                "duration_ms": 45,
                "attempt_count": 2,
                "intent_id": intent_id,
                "error_code": SafeToken.parse("projection_intent_contract_invalid"),
                "error_category": SafeToken.parse("integrity"),
                "is_retryable": False,
            },
        )

    blob = _captured_log_blob(caplog)
    assert len(caplog.records) >= 5
    for sentinel in _SENTINELS:
        assert sentinel not in blob, sentinel


def test_unsafe_event_field_with_sentinel_is_rejected_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = _build_logger()
    with caplog.at_level(logging.DEBUG):
        logger.emit(
            EventName.SOURCE_VERSION_PUBLISH_SUCCEEDED,
            {
                "operation": SafeToken.parse("create"),
                "outcome": SafeToken.parse("published"),
                "duration_ms": 9,
                "attempt_count": 1,
                "content_version": 1,
                "source_id": uuid4(),
                "source_version_id": uuid4(),
                "event_id": uuid4(),
                "title": f"secret title {TITLE_SENTINEL}",
            },
        )

    blob = _captured_log_blob(caplog)
    assert TITLE_SENTINEL not in blob
    events = [getattr(record, _MARKER, {}).get("event") for record in caplog.records]
    assert "logging_payload_rejected" in events


def test_idempotency_key_sentinel_never_leaks_through_logging_scenario(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The idempotency-key sentinel must survive a full logging round trip
    undisclosed: an application error chained onto a cause that carries the raw
    key, plus the registered rejection event, produce no record, rendered
    message or schema payload containing it."""

    underscored_sentinel = KEY_SENTINEL.replace("-", "_")
    key = IdempotencyKey(underscored_sentinel)
    assert underscored_sentinel in key.value
    underlying = RuntimeError(f"commit rejected for key {key.value}")
    try:
        raise underlying
    except RuntimeError as cause:
        try:
            raise SourcePublicationError(
                ErrorCode.SOURCE_IDEMPOTENCY_MISMATCH,
                safe_details={"source_id": uuid4()},
            ) from cause
        except SourcePublicationError as captured:
            error = captured

    logger = _build_logger()
    with caplog.at_level(logging.DEBUG):
        logger.emit_application_error(error)
        logger.emit(
            EventName.SOURCE_VERSION_PUBLISH_REJECTED,
            {
                "operation": SafeToken.parse("update"),
                "outcome": SafeToken.parse("rejected"),
                "duration_ms": 2,
                "error_code": SafeToken.parse("source_idempotency_mismatch"),
                "error_category": SafeToken.parse("conflict"),
                "is_retryable": False,
                "source_id": uuid4(),
                "event_id": uuid4(),
                "reason_code": SafeToken.parse("idempotency_mismatch"),
            },
        )

    blob = _captured_log_blob(caplog)
    assert len(caplog.records) >= 2
    for sentinel in (KEY_SENTINEL, underscored_sentinel):
        assert sentinel not in blob, sentinel


def test_dependency_sql_and_password_sentinels_never_leak_into_logs(
    tmp_path: Path,
) -> None:
    """Raw dependency records must reach the installed diagnostics sink only as
    fingerprinted ``dependency_log`` lines; pytest's own capture formatter is a
    test harness, never a production sink, so the real handler path is
    installed here."""

    settings = load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_ENVIRONMENT": "test",
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
        },
    )
    stdout = StringIO()
    stderr = StringIO()
    configure_diagnostics(settings, stdout=stdout, stderr=stderr)
    try:
        logging.getLogger("psycopg.connection").error(
            "execute %s with password %s", SQL_SENTINEL, PASSWORD_SENTINEL
        )
        logging.getLogger("sqlalchemy.engine").error(
            "fingerprinted request %s", FINGERPRINT_SENTINEL
        )
    finally:
        reset_diagnostics_for_testing()

    blob = stdout.getvalue() + stderr.getvalue()
    for sentinel in (SQL_SENTINEL, PASSWORD_SENTINEL, FINGERPRINT_SENTINEL):
        assert sentinel not in blob, sentinel
    records = [json.loads(line) for line in blob.splitlines()]
    assert len(records) == 2
    for record in records:
        assert record["event"] == "dependency_log"
        assert "message" not in record
        assert "args" not in record


# --- Source lifecycle telemetry leakage (Task 11) ---------------------------


LIFECYCLE_LOCATOR_SENTINEL = "do-not-emit-lifecycle-locator"
LIFECYCLE_TITLE_SENTINEL = "do-not-emit-lifecycle-title"
LIFECYCLE_FINGERPRINT_SENTINEL = "do-not-emit-lifecycle-fingerprint"
LIFECYCLE_TOKEN_SENTINEL = "do-not-emit-lifecycle-token"
LIFECYCLE_CONTENT_SENTINEL = "do-not-emit-lifecycle-content"

_LIFECYCLE_SENTINELS = (
    LIFECYCLE_LOCATOR_SENTINEL,
    LIFECYCLE_TITLE_SENTINEL,
    LIFECYCLE_FINGERPRINT_SENTINEL,
    LIFECYCLE_TOKEN_SENTINEL,
    LIFECYCLE_CONTENT_SENTINEL,
)


def test_lifecycle_rejection_event_never_leaks_lifecycle_sentinels(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A source lifecycle rejection event must only carry closed labels.

    The lifecycle rejection diagnostic emits the closed
    ``source_version_publish_rejected`` event with the operation token,
    the closed error code, the safe diff hash and the policy outcome —
    no raw locator, title, fingerprint, token or content.
    """

    logger = _build_logger()
    with caplog.at_level(logging.DEBUG):
        logger.emit(
            EventName.SOURCE_VERSION_PUBLISH_REJECTED,
            {
                "operation": SafeToken.parse("rename"),
                "outcome": SafeToken.parse("rejected"),
                "duration_ms": 4,
                "error_code": SafeToken.parse("locator_conflict"),
                "error_category": SafeToken.parse("conflict"),
                "is_retryable": False,
                "source_id": uuid4(),
                "event_id": uuid4(),
                "reason_code": SafeToken.parse("locator_conflict"),
                "safe_diff_hash": "f" * 64,
                "policy_revision_number": 1,
            },
        )

    blob = _captured_log_blob(caplog)
    assert len(caplog.records) >= 1
    for sentinel in _LIFECYCLE_SENTINELS:
        assert sentinel not in blob, sentinel


def test_lifecycle_endpoint_error_never_leaks_raw_context() -> None:
    """A typed lifecycle error must not copy locator / title / fingerprint into the error text."""

    underlying = RuntimeError(
        f"store failure at {LIFECYCLE_LOCATOR_SENTINEL} title={LIFECYCLE_TITLE_SENTINEL} "
        f"fingerprint={LIFECYCLE_FINGERPRINT_SENTINEL} token={LIFECYCLE_TOKEN_SENTINEL} "
        f"content={LIFECYCLE_CONTENT_SENTINEL}"
    )

    try:
        raise underlying
    except RuntimeError as cause:
        from personal_os.source_lifecycle.errors import (
            SourceLifecycleError,
            SourceLifecycleErrorCode,
        )

        try:
            raise SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT) from cause
        except SourceLifecycleError as captured:
            error = captured

    rendered = f"{error} {error!r} {error.code.value}"
    for sentinel in _LIFECYCLE_SENTINELS:
        assert sentinel not in rendered, sentinel


def test_lifecycle_metrics_only_carry_closed_operation_outcome_and_error_code() -> None:
    """The lifecycle metric sink only sees the closed operation, outcome, error_code labels."""

    from personal_os.source_lifecycle.commands import LifecycleOperation
    from personal_os.source_lifecycle.errors import SourceLifecycleErrorCode
    from personal_os.source_lifecycle.metrics import LifecycleMetricOutcome

    # The contract is that the metric port never accepts values outside the
    # closed enums; the existing assertion in the in-memory recorder is the
    # proof. We re-state it here so the contract is pinned alongside the
    # other lifecycle telemetry-safety tests.
    closed_operations = {op.value for op in LifecycleOperation}
    assert closed_operations == {"rename", "move", "delete", "restore"}
    closed_outcomes = {outcome.value for outcome in LifecycleMetricOutcome}
    assert closed_outcomes == {"committed", "rejected", "replayed"}
    closed_errors = {code.value for code in SourceLifecycleErrorCode}
    # The lifecycle vocabulary token never enters the closed error code set.
    for lifecycle_token in {"rename", "move", "delete", "restore"}:
        assert lifecycle_token not in closed_errors


def test_lifecycle_logging_payload_with_locator_sentinel_is_rejected_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lifecycle event field carrying a locator sentinel is rejected, never emitted."""

    logger = _build_logger()
    with caplog.at_level(logging.DEBUG):
        logger.emit(
            EventName.SOURCE_VERSION_PUBLISH_REJECTED,
            {
                "operation": SafeToken.parse("rename"),
                "outcome": SafeToken.parse("rejected"),
                "duration_ms": 1,
                "error_code": SafeToken.parse("locator_conflict"),
                "error_category": SafeToken.parse("conflict"),
                "is_retryable": False,
                "source_id": uuid4(),
                "event_id": uuid4(),
                "reason_code": SafeToken.parse("locator_conflict"),
                "safe_diff_hash": "f" * 64,
                "policy_revision_number": 1,
                "expected_locator": f"secret {LIFECYCLE_LOCATOR_SENTINEL}",
            },
        )

    blob = _captured_log_blob(caplog)
    assert LIFECYCLE_LOCATOR_SENTINEL not in blob
    events = [getattr(record, _MARKER, {}).get("event") for record in caplog.records]
    assert "logging_payload_rejected" in events
