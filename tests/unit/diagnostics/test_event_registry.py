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
