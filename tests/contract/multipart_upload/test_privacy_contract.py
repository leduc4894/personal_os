"""Multipart upload privacy contract: closed diagnostics, no identifier leak.

Task 11 of the resumable multipart mobile-upload child (spec 7 and 9.3):
every safe surface the multipart diagnostics own — the rendered typed-error
text, the metric contract label names and their closed label values, the
rejection-ring snapshot, the structured rejection event definition and the
published OpenAPI document — carries ONLY closed low-cardinality tokens.
Sensitive multipart sentinels (a provider ETag or upload ID, a session or
request ID value, a Vault path, a full digest, a signed URL or signature,
a staging key and provider exception text) must be absent from every
rendered surface; identifier-bearing metric label names must be rejected by
the metric-contract validator; and a cleanup failure's closed reason must
land readable on the rejection ring.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.metrics import (
    MULTIPART_METRIC_CONTRACTS,
    MULTIPART_METRIC_LABEL_NAMES,
    InMemoryMultipartUploadMetrics,
    MultipartCleanupOutcome,
    MultipartCompletionOutcome,
    MultipartMetricFlow,
    MultipartRejectionReason,
    MultipartSessionOutcome,
    validate_multipart_metric_contracts,
)

_OPENAPI_SNAPSHOT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "packages" / "api-client" / "openapi.json"
)

_PLUGIN_CLOSED_VOCABULARY_SOURCES: Final[tuple[Path, ...]] = (
    Path(__file__).resolve().parents[3]
    / "apps"
    / "obsidian-plugin"
    / "src"
    / "journal"
    / "sync-diagnostics-trail.ts",
    Path(__file__).resolve().parents[3]
    / "apps"
    / "obsidian-plugin"
    / "src"
    / "journal"
    / "status.ts",
    Path(__file__).resolve().parents[3]
    / "apps"
    / "obsidian-plugin"
    / "src"
    / "journal"
    / "contracts.ts",
)

#: Sensitive multipart sentinels (spec 9.3) injected by the leak tests: a
#: provider ETag, a provider upload ID, a session-ID value, a request-ID
#: value, a Vault path, a full content digest, a signed URL with its
#: signature, a staging key and provider exception text. None may appear
#: on any safe multipart surface.
SENSITIVE_MULTIPART_SENTINELS: Final[tuple[str, ...]] = (
    "sentinel-etag-9f8e7d6c",
    "sentinel-provider-upload-id-000111222333",
    "sentinel-session-id-value-445566778899",
    "sentinel-request-id-value-998877665544",
    "notes/sentinel-leak-path.md",
    "sentinel-digest-hex-0123456789abcdef0123456789abcdef",
    "https://sentinel-storage.example.com/staging?X-Amz-Signature=SENTINELSIGNATURE",
    "staging/sentinel-key-0f1e2d3c4b5a",
    "SentinelProviderException: multipart sentinel failure",
    "SENTINEL-SIGNATURE-VALUE",
)

#: Metric label names that would carry an identifier, locator or other
#: high-cardinality value (spec 7): every one of them must be rejected by
#: the multipart metric-contract validator.
IDENTIFIER_BEARING_METRIC_LABEL_NAMES: Final[tuple[str, ...]] = (
    "session_id",
    "staging_key",
    "provider_upload_id",
    "etag",
    "request_id",
    "path",
    "locator",
    "digest",
    "url",
    "signature",
    "workspace_id",
    "device_id",
)


def _rendered_typed_error_text() -> str:
    """Render every closed multipart registry error once."""
    parts: list[str] = []
    for error_code in sorted(MultipartUploadError.allowed_codes, key=lambda code: code.value):
        try:
            raise MultipartUploadError(error_code)
        except ApplicationError as error:
            parts.append(str(error))
            parts.append(repr(error))
    return "\n".join(parts)


def _rendered_metrics_surface() -> str:
    """Render the metric contracts, their label values and a ring snapshot."""
    recorder = InMemoryMultipartUploadMetrics(epoch_ms_clock=lambda: 1_784_000_000_000)
    recorder.record_session(outcome=MultipartSessionOutcome.CREATED, duration_seconds=0.1)
    recorder.record_completion(outcome=MultipartCompletionOutcome.COMMITTED, duration_seconds=0.2)
    recorder.record_cleanup(outcome=MultipartCleanupOutcome.FAILED)
    for flow, reason in (
        (MultipartMetricFlow.SESSION_CREATE, MultipartRejectionReason.MULTIPART_SESSION_NOT_FOUND),
        (MultipartMetricFlow.CLEANUP, MultipartRejectionReason.MULTIPART_CLEANUP_FAILED),
    ):
        recorder.record_rejection(flow=flow, reason_code=reason)
    diagnostics = recorder.rejection_diagnostics()
    return "\n".join(
        [
            repr(dict(MULTIPART_METRIC_CONTRACTS)),
            repr(sorted(MULTIPART_METRIC_LABEL_NAMES)),
            repr(diagnostics),
            repr(diagnostics.recent_rejections),
        ]
    )


@pytest.fixture
def rendered() -> str:
    """Render EVERY safe multipart surface into one scannable blob."""

    parts: list[str] = [
        _rendered_typed_error_text(),
        _rendered_metrics_surface(),
        _OPENAPI_SNAPSHOT_PATH.read_text(encoding="utf-8"),
        json.dumps(json.loads(_OPENAPI_SNAPSHOT_PATH.read_text(encoding="utf-8"))),
    ]
    for source_path in _PLUGIN_CLOSED_VOCABULARY_SOURCES:
        parts.append(source_path.read_text(encoding="utf-8"))
    return "\n".join(parts)


@pytest.fixture
def metrics() -> InMemoryMultipartUploadMetrics:
    return InMemoryMultipartUploadMetrics(epoch_ms_clock=lambda: 1_784_000_000_000)


# --- the sentinel scan across every safe surface -------------------------------------------------


def test_multipart_sensitive_sentinels_are_absent_from_all_safe_surfaces(rendered: str) -> None:
    for sentinel in SENSITIVE_MULTIPART_SENTINELS:
        assert sentinel not in rendered, sentinel


def test_openapi_snapshot_carries_no_multipart_identity_marker() -> None:
    document: dict[str, Any] = json.loads(_OPENAPI_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    rendered = json.dumps(document)
    for forbidden_marker in ("provider_upload_id", "staging_key", "rejection_diagnostics"):
        assert forbidden_marker not in rendered


# --- the closed metric label universe (spec 7) ----------------------------------------------------


def test_metric_label_names_are_exactly_the_closed_five() -> None:
    assert (
        frozenset({"outcome", "state", "platform_class", "stage", "error_code"})
        == MULTIPART_METRIC_LABEL_NAMES
    )


def test_every_metric_contract_label_is_within_the_closed_universe() -> None:
    validate_multipart_metric_contracts()
    for labels in MULTIPART_METRIC_CONTRACTS.values():
        assert labels <= MULTIPART_METRIC_LABEL_NAMES


@pytest.mark.parametrize("label_name", IDENTIFIER_BEARING_METRIC_LABEL_NAMES)
def test_identifier_bearing_metric_label_names_are_rejected(label_name: str) -> None:
    with pytest.raises(ValueError, match="closed metric label name"):
        validate_multipart_metric_contracts({"multipart_probe_total": frozenset({label_name})})


def test_the_rejected_label_name_is_reported_not_the_label_value() -> None:
    with pytest.raises(ValueError, match="session_id") as raised:
        validate_multipart_metric_contracts({"multipart_probe_total": frozenset({"session_id"})})
    assert "multipart_probe_total" in str(raised.value)


@pytest.mark.parametrize("sentinel", SENSITIVE_MULTIPART_SENTINELS)
def test_the_recorder_rejects_sentinel_label_values(sentinel: str) -> None:
    recorder = InMemoryMultipartUploadMetrics()
    with pytest.raises(ValueError):
        recorder.record_session(outcome=sentinel, duration_seconds=0.1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        recorder.record_rejection(
            flow=sentinel,  # type: ignore[arg-type]
            reason_code=MultipartRejectionReason.MULTIPART_CLEANUP_FAILED,
        )
    with pytest.raises(ValueError):
        recorder.record_rejection(
            flow=MultipartMetricFlow.CLEANUP,
            reason_code=sentinel,  # type: ignore[arg-type]
        )


# --- the readable closed cleanup reason (spec 8: cleanup-failure row) -----------------------------


def test_cleanup_failure_records_closed_reason(metrics: InMemoryMultipartUploadMetrics) -> None:
    metrics.record_cleanup_failed(ErrorCode.MULTIPART_CLEANUP_FAILED)
    recent = metrics.rejection_diagnostics().recent_rejections
    assert recent[-1].error_code is MultipartRejectionReason.MULTIPART_CLEANUP_FAILED
    assert recent[-1].error_code == ErrorCode.MULTIPART_CLEANUP_FAILED
    assert recent[-1].flow is MultipartMetricFlow.CLEANUP


def test_cleanup_failure_rejects_codes_outside_the_multipart_block(
    metrics: InMemoryMultipartUploadMetrics,
) -> None:
    with pytest.raises(ValueError):
        metrics.record_cleanup_failed(ErrorCode.SOURCE_VERSION_CONFLICT)
    assert metrics.rejection_diagnostics().recent_rejections == ()
