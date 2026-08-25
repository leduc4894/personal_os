"""Closed diagnostic event registry guard and the source-publication events.

The completeness guard proves every registered :class:`EventName` carries a
definition whose required fields are a subset of its allowed fields. The
source-publication section pins the exact six event contracts added by the
source version publication spec: publish success/replay/rejection and
projection dispatch success/failure/lease reclaim.
"""

from __future__ import annotations

from personal_os.diagnostics.events import (
    EVENT_DEFINITIONS,
    DiagnosticLevel,
    EventName,
    ResultCode,
)


def test_event_registry_is_complete_and_well_formed() -> None:
    """Every EventName member has exactly one definition with required ⊆ allowed."""

    assert set(EVENT_DEFINITIONS) == set(EventName)
    for event_name, definition in EVENT_DEFINITIONS.items():
        assert definition.required_fields <= definition.allowed_fields, event_name
        assert definition.allowed_fields, event_name


EXPECTED_FIELDS = frozenset({"http_method", "route", "status_code", "duration_ms"})


def test_api_request_events_have_closed_low_cardinality_fields() -> None:
    for name, result in (
        (EventName.API_REQUEST_COMPLETED, ResultCode.SUCCEEDED),
        (EventName.API_REQUEST_REJECTED, ResultCode.REJECTED),
        (EventName.API_REQUEST_FAILED, ResultCode.FAILED),
    ):
        definition = EVENT_DEFINITIONS[name]
        assert definition.result_code is result
        assert definition.required_fields == EXPECTED_FIELDS
        assert definition.allowed_fields == EXPECTED_FIELDS


def test_source_publication_events_are_registered_with_exact_contracts() -> None:
    expected = {
        EventName.SOURCE_VERSION_PUBLISH_SUCCEEDED: (
            DiagnosticLevel.INFO,
            ResultCode.SUCCEEDED,
            frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "content_version",
                    "source_id",
                    "source_version_id",
                    "event_id",
                }
            ),
            frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "content_version",
                    "source_id",
                    "source_version_id",
                    "event_id",
                }
            ),
        ),
        EventName.SOURCE_VERSION_PUBLISH_REPLAYED: (
            DiagnosticLevel.INFO,
            ResultCode.SUCCEEDED,
            frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "content_version",
                    "source_id",
                    "source_version_id",
                    "event_id",
                }
            ),
            frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "content_version",
                    "source_id",
                    "source_version_id",
                    "event_id",
                }
            ),
        ),
        EventName.SOURCE_VERSION_PUBLISH_REJECTED: (
            DiagnosticLevel.WARNING,
            ResultCode.REJECTED,
            frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
            frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "error_code",
                    "error_category",
                    "is_retryable",
                    "source_id",
                    "event_id",
                    "reason_code",
                }
            ),
        ),
        EventName.SOURCE_VERSION_PUBLISH_FAILED: (
            DiagnosticLevel.ERROR,
            ResultCode.FAILED,
            frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
            frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "error_code",
                    "error_category",
                    "is_retryable",
                    "source_id",
                    "event_id",
                }
            ),
        ),
        EventName.PROJECTION_INTENT_DISPATCHED: (
            DiagnosticLevel.INFO,
            ResultCode.SUCCEEDED,
            frozenset(
                {
                    "projection_kind",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "intent_id",
                }
            ),
            frozenset(
                {
                    "projection_kind",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "intent_id",
                }
            ),
        ),
        EventName.PROJECTION_INTENT_DISPATCH_FAILED: (
            DiagnosticLevel.ERROR,
            ResultCode.FAILED,
            frozenset(
                {
                    "projection_kind",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "intent_id",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
            frozenset(
                {
                    "projection_kind",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "intent_id",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
        ),
        EventName.PROJECTION_INTENT_LEASE_RECLAIMED: (
            DiagnosticLevel.WARNING,
            ResultCode.DEGRADED,
            frozenset({"projection_kind", "count"}),
            frozenset({"projection_kind", "count", "attempt_count"}),
        ),
    }
    for event_name, (level, result_code, required, allowed) in expected.items():
        definition = EVENT_DEFINITIONS[event_name]
        assert (definition.level, definition.result_code) == (level, result_code), event_name
        assert definition.required_fields == required, event_name
        assert definition.allowed_fields == allowed, event_name


def test_policy_dispatch_unavailable_events_are_registered_with_exact_contracts() -> None:
    """Worker dispatch-unavailable events: closed ids, counts and fingerprints only.

    The two events of the policy worker dispatch loops (spec 10/15) fire at
    the unexpected-start catches whose lease outcome stays unknown: the fields
    carry the opaque row id, the attempt count and the closed
    exception-type/stack-fingerprint reductions — never provider text, a
    workflow identity or any exception argument.
    """

    expected = {
        EventName.PREVIEW_DISPATCH_UNAVAILABLE: frozenset(
            {"policy_preview_id", "attempt_count", "exception_type", "stack_fingerprint"}
        ),
        EventName.RECONCILIATION_DISPATCH_UNAVAILABLE: frozenset(
            {
                "policy_reconciliation_intent_id",
                "attempt_count",
                "exception_type",
                "stack_fingerprint",
            }
        ),
    }
    for event_name, fields in expected.items():
        definition = EVENT_DEFINITIONS[event_name]
        assert definition.level is DiagnosticLevel.ERROR, event_name
        assert definition.result_code is ResultCode.FAILED, event_name
        assert definition.required_fields == fields, event_name
        assert definition.allowed_fields == fields, event_name


def test_device_sync_operation_events_are_registered_with_exact_contracts() -> None:
    """Device-sync operation events: closed operation/reason labels and duration only.

    Success carries exactly the operation and duration; rejection and failure
    carry exactly the operation, the closed reason code and the duration (spec
    14.2 of the device cursor and manifest reconciliation design). No
    identifier, locator, digest or provider detail is ever a field.
    """

    expected = {
        EventName.DEVICE_SYNC_OPERATION_COMPLETED: (
            DiagnosticLevel.INFO,
            ResultCode.SUCCEEDED,
            frozenset({"operation", "duration_ms"}),
            frozenset({"operation", "duration_ms"}),
        ),
        EventName.DEVICE_SYNC_OPERATION_REJECTED: (
            DiagnosticLevel.WARNING,
            ResultCode.REJECTED,
            frozenset({"operation", "reason", "duration_ms"}),
            frozenset({"operation", "reason", "duration_ms"}),
        ),
        EventName.DEVICE_SYNC_OPERATION_FAILED: (
            DiagnosticLevel.ERROR,
            ResultCode.FAILED,
            frozenset({"operation", "reason", "duration_ms"}),
            frozenset({"operation", "reason", "duration_ms"}),
        ),
    }
    for event_name, (level, result_code, required, allowed) in expected.items():
        definition = EVENT_DEFINITIONS[event_name]
        assert (definition.level, definition.result_code) == (level, result_code), event_name
        assert definition.required_fields == required, event_name
        assert definition.allowed_fields == allowed, event_name


def test_exclusion_policy_evaluation_events_are_registered_with_exact_contracts() -> None:
    """Spec 21 evaluation events: closed boundary/decision labels and counts only.

    Field values stay inside the spec 21 allowed vocabulary — boundary,
    decision, revision number, rule/matched/missing counts and duration — and
    never include a locator, operand, path or subject fingerprint.
    """

    expected = {
        EventName.EXCLUSION_POLICY_EVALUATION_COMPLETED: (
            DiagnosticLevel.INFO,
            ResultCode.SUCCEEDED,
            frozenset({"boundary", "decision", "rule_count"}),
            frozenset(
                {
                    "boundary",
                    "decision",
                    "rule_count",
                    "revision_number",
                    "duration_ms",
                    "matched_rule_count",
                    "missing_field_count",
                }
            ),
        ),
        EventName.EXCLUSION_POLICY_EVALUATION_REJECTED: (
            DiagnosticLevel.WARNING,
            ResultCode.REJECTED,
            frozenset({"boundary", "error_code"}),
            frozenset(
                {
                    "boundary",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
        ),
    }
    for event_name, (level, result_code, required, allowed) in expected.items():
        definition = EVENT_DEFINITIONS[event_name]
        assert (definition.level, definition.result_code) == (level, result_code), event_name
        assert definition.required_fields == required, event_name
        assert definition.allowed_fields == allowed, event_name
