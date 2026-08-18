"""Closed small-file sync contracts: shapes, grammars and outcome mappings.

Asserts the frozen 16 MiB single-part limit (equality allowed, overage
rejected), the strict UUID idempotency grammar, the create-versus-update field
requirements of spec 10.1, the canonical locator grammar mirrored from the
plugin policy evaluator, the opaque URL-safe operation-token grammar, the
terminal preflight-outcome mapping of spec 10.1/12 and the closed low-cardinality
metric labels that never accept identifiers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from personal_os.object_storage import CanonicalMediaType, ContentDigest
from personal_os.small_file_sync.contracts import (
    MAX_SINGLE_PART_FILE_SIZE_BYTES,
    TERMINAL_PREFLIGHT_OUTCOMES,
    NormalizedLocator,
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFilePreflightOutcome,
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
    SmallFileUploadOperation,
    UploadOperationToken,
)
from personal_os.small_file_sync.metrics import (
    SMALL_FILE_METRIC_CONTRACTS,
    InMemorySmallFileSyncMetrics,
    SmallFileMetricOutcome,
    SmallFileRejectionReason,
)

_EVENT_ID = uuid4()
_LOCAL_FILE_ID = uuid4()
_DECLARED_DIGEST = ContentDigest.parse("ab" * 32)
_DECLARED_MEDIA_TYPE = CanonicalMediaType.parse("text/markdown")
_POLICY_REVISION_NUMBER = 4
_EXPIRES_AT = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC) + timedelta(minutes=10)


def _create_preflight(
    *,
    size_bytes: int = 128,
    source_id: UUID | None = None,
    base_version_id: UUID | None = None,
    idempotency_key: SmallFileIdempotencyKey | None = None,
) -> SmallFilePreflight:
    return SmallFilePreflight(
        event_id=_EVENT_ID,
        idempotency_key=idempotency_key or SmallFileIdempotencyKey(str(uuid4())),
        operation=SmallFileOperation.CREATE,
        local_file_id=_LOCAL_FILE_ID,
        source_id=source_id,
        base_version_id=base_version_id,
        normalized_locator=NormalizedLocator("notes/planning.md"),
        sha256=_DECLARED_DIGEST,
        size_bytes=size_bytes,
        media_type=_DECLARED_MEDIA_TYPE,
        policy_revision_number=_POLICY_REVISION_NUMBER,
    )


def _update_preflight(
    *,
    source_id: UUID | None = None,
    base_version_id: UUID | None = None,
) -> SmallFilePreflight:
    return SmallFilePreflight(
        event_id=_EVENT_ID,
        idempotency_key=SmallFileIdempotencyKey(str(uuid4())),
        operation=SmallFileOperation.UPDATE,
        local_file_id=_LOCAL_FILE_ID,
        source_id=source_id,
        base_version_id=base_version_id,
        normalized_locator=NormalizedLocator("notes/planning.md"),
        sha256=_DECLARED_DIGEST,
        size_bytes=64,
        media_type=_DECLARED_MEDIA_TYPE,
        policy_revision_number=_POLICY_REVISION_NUMBER,
    )


def _upload_operation(
    preflight: SmallFilePreflight | None = None,
    *,
    reserved_source_id: UUID | None = None,
    expires_at: datetime = _EXPIRES_AT,
) -> SmallFileUploadOperation:
    return SmallFileUploadOperation(
        operation_token=UploadOperationToken("Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1"),
        preflight=preflight or _create_preflight(),
        device_context=SmallFileDeviceContext(device_id=uuid4(), workspace_id=uuid4()),
        reserved_source_id=reserved_source_id,
        expires_at=expires_at,
    )


# --- frozen 16 MiB single-part limit ------------------------------------------------------


def test_single_part_limit_is_exactly_sixteen_mib() -> None:
    assert MAX_SINGLE_PART_FILE_SIZE_BYTES == 16 * 1024 * 1024


def test_preflight_accepts_size_equal_to_the_single_part_limit() -> None:
    preflight = _create_preflight(size_bytes=MAX_SINGLE_PART_FILE_SIZE_BYTES)
    assert preflight.size_bytes == MAX_SINGLE_PART_FILE_SIZE_BYTES


def test_preflight_rejects_size_one_byte_over_the_single_part_limit() -> None:
    with pytest.raises(ValueError, match="single-part size limit"):
        _create_preflight(size_bytes=MAX_SINGLE_PART_FILE_SIZE_BYTES + 1)


def test_preflight_rejects_negative_size() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        _create_preflight(size_bytes=-1)


# --- strict UUID idempotency grammar -------------------------------------------------------


def test_idempotency_key_accepts_canonical_uuid_text() -> None:
    canonical = str(uuid4())
    key = SmallFileIdempotencyKey(canonical)
    assert key.value == canonical
    assert key.value.isprintable()


@pytest.mark.parametrize(
    "value",
    [
        str(uuid4()).upper(),
        "{" + str(uuid4()) + "}",
        "urn:uuid:" + str(uuid4()),
        " " + str(uuid4()) + " ",
        str(uuid4()) + "\n",
        str(uuid4()).replace("-", ""),
        str(uuid4())[:-1],
        "not-a-uuid",
        "00000000-0000-0000-0000-00000000000\u0000",
        "",
    ],
)
def test_idempotency_key_rejects_non_canonical_text(value: str) -> None:
    with pytest.raises(ValueError, match="canonical lowercase hyphenated UUID"):
        SmallFileIdempotencyKey(value)


def test_idempotency_key_rejects_nil_uuid() -> None:
    with pytest.raises(ValueError, match="non-nil"):
        SmallFileIdempotencyKey("00000000-0000-0000-0000-000000000000")


# --- create versus update field requirements (spec 10.1) -----------------------------------


def test_create_preflight_requires_absent_source_and_base() -> None:
    preflight = _create_preflight()
    assert preflight.source_id is None
    assert preflight.base_version_id is None


def test_create_preflight_rejects_source_id() -> None:
    with pytest.raises(ValueError, match="create preflight must not carry a source_id"):
        _create_preflight(source_id=uuid4())


def test_create_preflight_rejects_base_version_id() -> None:
    with pytest.raises(ValueError, match="create preflight must not carry a base_version_id"):
        _create_preflight(base_version_id=uuid4())


def test_update_preflight_requires_source_and_base() -> None:
    source_id = uuid4()
    base_version_id = uuid4()
    preflight = _update_preflight(source_id=source_id, base_version_id=base_version_id)
    assert preflight.source_id == source_id
    assert preflight.base_version_id == base_version_id


def test_update_preflight_rejects_missing_source_id() -> None:
    with pytest.raises(ValueError, match="update preflight requires a source_id"):
        _update_preflight(base_version_id=uuid4())


def test_update_preflight_rejects_missing_base_version_id() -> None:
    with pytest.raises(ValueError, match="update preflight requires a base_version_id"):
        _update_preflight(source_id=uuid4())


def test_update_preflight_rejects_nil_source_ids() -> None:
    with pytest.raises(ValueError, match="non-nil UUID"):
        _update_preflight(source_id=UUID(int=0), base_version_id=uuid4())
    with pytest.raises(ValueError, match="non-nil UUID"):
        _update_preflight(source_id=uuid4(), base_version_id=UUID(int=0))


def test_preflight_rejects_nil_event_and_local_file_ids() -> None:
    nil_id = UUID(int=0)
    with pytest.raises(ValueError, match="event_id must be a non-nil UUID"):
        SmallFilePreflight(
            event_id=nil_id,
            idempotency_key=SmallFileIdempotencyKey(str(uuid4())),
            operation=SmallFileOperation.CREATE,
            local_file_id=_LOCAL_FILE_ID,
            source_id=None,
            base_version_id=None,
            normalized_locator=NormalizedLocator("notes/planning.md"),
            sha256=_DECLARED_DIGEST,
            size_bytes=1,
            media_type=_DECLARED_MEDIA_TYPE,
            policy_revision_number=_POLICY_REVISION_NUMBER,
        )
    with pytest.raises(ValueError, match="local_file_id must be a non-nil UUID"):
        SmallFilePreflight(
            event_id=_EVENT_ID,
            idempotency_key=SmallFileIdempotencyKey(str(uuid4())),
            operation=SmallFileOperation.CREATE,
            local_file_id=nil_id,
            source_id=None,
            base_version_id=None,
            normalized_locator=NormalizedLocator("notes/planning.md"),
            sha256=_DECLARED_DIGEST,
            size_bytes=1,
            media_type=_DECLARED_MEDIA_TYPE,
            policy_revision_number=_POLICY_REVISION_NUMBER,
        )


def test_preflight_rejects_non_positive_policy_revision_number() -> None:
    with pytest.raises(ValueError, match="policy_revision_number"):
        SmallFilePreflight(
            event_id=_EVENT_ID,
            idempotency_key=SmallFileIdempotencyKey(str(uuid4())),
            operation=SmallFileOperation.CREATE,
            local_file_id=_LOCAL_FILE_ID,
            source_id=None,
            base_version_id=None,
            normalized_locator=NormalizedLocator("notes/planning.md"),
            sha256=_DECLARED_DIGEST,
            size_bytes=1,
            media_type=_DECLARED_MEDIA_TYPE,
            policy_revision_number=0,
        )


# --- canonical normalized locator grammar -------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "notes/planning.md",
        "a/b/c.png",
        "assets/attachments/hoá-đơn.pdf",
        "journal/2026/2026-08-18.md",
    ],
)
def test_locator_accepts_canonical_normalized_paths(value: str) -> None:
    assert NormalizedLocator(value).value == value


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("", "non-empty"),
        ("notes\\planning.md", "backslash"),
        ("/absolute/path.md", "absolute"),
        ("notes/", "trailing separator"),
        ("notes//planning.md", "segment"),
        ("notes/../planning.md", "segment"),
        ("notes/./planning.md", "segment"),
        ("C:/notes/plan.md", "scheme or drive"),
        ("notes/pla\u0000nning.md", "control"),
        ("notes/planning.md\t", "control"),
        ("notes/cafe\u0301.md", "NFC"),
        ("/".join(["deep"] * 257) + ".md", "segments"),
        ("x/" * 255 + "y" * 4000, "UTF-8 bytes"),
    ],
)
def test_locator_rejects_non_canonical_paths(value: str, pattern: str) -> None:
    with pytest.raises(ValueError, match=pattern):
        NormalizedLocator(value)


def test_locator_accepts_the_segment_count_limit_exactly() -> None:
    value = "/".join(["deep"] * 256) + ".md"
    assert NormalizedLocator(value).value == value


# --- opaque URL-safe operation-token grammar ----------------------------------------------


def test_operation_token_accepts_opaque_url_safe_grammar() -> None:
    value = "Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1"
    token = UploadOperationToken(value)
    assert token.value == value


def test_operation_token_accepts_minimum_and_maximum_lengths() -> None:
    assert UploadOperationToken("a" * 32).value == "a" * 32
    assert UploadOperationToken("A" * 128).value == "A" * 128


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("a" * 31, "32 to 128"),
        ("a" * 129, "32 to 128"),
        ("Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1=", "URL-safe"),
        ("Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1+", "URL-safe"),
        ("Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1/", "URL-safe"),
        ("Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1:", "URL-safe"),
        ("Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1 ", "printable"),
        ("Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1\n", "printable"),
    ],
)
def test_operation_token_rejects_out_of_grammar_text(value: str, pattern: str) -> None:
    with pytest.raises(ValueError, match=pattern):
        UploadOperationToken(value)


def test_operation_token_rejects_raw_uuid_text() -> None:
    with pytest.raises(ValueError, match="URL-safe"):
        UploadOperationToken(str(uuid4()))


# --- upload-operation binding --------------------------------------------------------------


def test_upload_operation_binds_a_create_reservation_without_canonical_source() -> None:
    reserved_source_id = uuid4()
    operation = _upload_operation(reserved_source_id=reserved_source_id)
    assert operation.preflight.operation is SmallFileOperation.CREATE
    assert operation.reserved_source_id == reserved_source_id


def test_upload_operation_accepts_create_without_reservation() -> None:
    assert _upload_operation(reserved_source_id=None).reserved_source_id is None


def test_upload_operation_rejects_reserved_source_for_update() -> None:
    with pytest.raises(ValueError, match="update operation must not reserve a source_id"):
        _upload_operation(
            _update_preflight(source_id=uuid4(), base_version_id=uuid4()),
            reserved_source_id=uuid4(),
        )


def test_upload_operation_rejects_nil_reserved_source() -> None:
    with pytest.raises(ValueError, match="non-nil UUID"):
        _upload_operation(reserved_source_id=UUID(int=0))


def test_upload_operation_requires_aware_utc_expiry() -> None:
    naive = datetime(2026, 8, 18, 12, 10, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        _upload_operation(expires_at=naive)
    offset_zone = timezone(timedelta(hours=7))
    offset = datetime(2026, 8, 18, 19, 10, 0, tzinfo=offset_zone)
    operation = _upload_operation(expires_at=offset)
    assert operation.expires_at == offset.astimezone(UTC)


# --- terminal outcome mapping ---------------------------------------------------------------


def test_preflight_outcome_values_match_the_spec_text() -> None:
    assert {outcome.value for outcome in SmallFilePreflightOutcome} == {
        "committed_replay",
        "no_change",
        "excluded",
        "conflict",
        "single_part_upload",
    }
    assert {operation.value for operation in SmallFileOperation} == {"create", "update"}


def test_terminal_preflight_outcomes_are_exactly_the_four_finishers() -> None:
    expected = frozenset(
        {
            SmallFilePreflightOutcome.COMMITTED_REPLAY,
            SmallFilePreflightOutcome.NO_CHANGE,
            SmallFilePreflightOutcome.EXCLUDED,
            SmallFilePreflightOutcome.CONFLICT,
        }
    )
    assert expected == TERMINAL_PREFLIGHT_OUTCOMES
    assert SmallFilePreflightOutcome.SINGLE_PART_UPLOAD not in TERMINAL_PREFLIGHT_OUTCOMES


# --- committed replay result values --------------------------------------------------------


def test_terminal_result_retains_the_canonical_commit_receipt() -> None:
    source_id = uuid4()
    version_id = uuid4()
    committed_at = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
    result = SmallFileTerminalResult(
        result_kind=SmallFileTerminalResultKind.COMMITTED,
        source_id=source_id,
        source_version_id=version_id,
        content_version=2,
        committed_at=committed_at,
    )
    assert result.source_id == source_id
    assert result.source_version_id == version_id
    assert result.committed_at == committed_at


def test_terminal_result_rejects_nil_ids_and_non_positive_version() -> None:
    committed_at = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="non-nil UUID"):
        SmallFileTerminalResult(
            result_kind=SmallFileTerminalResultKind.COMMITTED,
            source_id=UUID(int=0),
            source_version_id=uuid4(),
            content_version=1,
            committed_at=committed_at,
        )
    with pytest.raises(ValueError, match="positive"):
        SmallFileTerminalResult(
            result_kind=SmallFileTerminalResultKind.NO_CHANGE,
            source_id=uuid4(),
            source_version_id=uuid4(),
            content_version=0,
            committed_at=committed_at,
        )


def test_terminal_result_requires_aware_utc_commit_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SmallFileTerminalResult(
            result_kind=SmallFileTerminalResultKind.COMMITTED,
            source_id=uuid4(),
            source_version_id=uuid4(),
            content_version=1,
            committed_at=datetime(2026, 8, 18, 12, 0, 0),
        )


# --- server-derived device context ----------------------------------------------------------


def test_device_context_binds_credential_derived_identity() -> None:
    device_id = uuid4()
    workspace_id = uuid4()
    context = SmallFileDeviceContext(device_id=device_id, workspace_id=workspace_id)
    assert context.device_id == device_id
    assert context.workspace_id == workspace_id


def test_device_context_rejects_nil_ids() -> None:
    with pytest.raises(ValueError, match="device_id must be a non-nil UUID"):
        SmallFileDeviceContext(device_id=UUID(int=0), workspace_id=uuid4())
    with pytest.raises(ValueError, match="workspace_id must be a non-nil UUID"):
        SmallFileDeviceContext(device_id=uuid4(), workspace_id=UUID(int=0))


# --- closed metric labels --------------------------------------------------------------------


def test_metric_contracts_pin_exact_names_and_label_dimensions() -> None:
    expected = {
        "small_file_preflight_total": frozenset({"operation", "outcome"}),
        "small_file_preflight_duration_seconds": frozenset({"operation", "outcome"}),
        "small_file_upload_total": frozenset({"operation", "outcome"}),
        "small_file_upload_duration_seconds": frozenset({"operation", "outcome"}),
        "small_file_replay_total": frozenset({"operation"}),
        "small_file_rejection_total": frozenset({"operation", "reason_code"}),
    }
    assert expected == SMALL_FILE_METRIC_CONTRACTS


def test_metric_label_values_are_closed_enums() -> None:
    assert {outcome.value for outcome in SmallFileMetricOutcome} == {
        "committed",
        "integrity_failed",
        "rejected",
    }
    assert {reason.value for reason in SmallFileRejectionReason} == {
        "small_file_preflight_invalid",
        "small_file_operation_not_found",
        "small_file_operation_expired",
        "small_file_operation_identity_mismatch",
        "small_file_size_limit_exceeded",
        "small_file_content_integrity_failed",
        "small_file_upload_state_invalid",
    }


def test_in_memory_metrics_record_and_count_closed_labels() -> None:
    recorder = InMemorySmallFileSyncMetrics()
    recorder.record_preflight(
        operation=SmallFileOperation.CREATE,
        outcome=SmallFilePreflightOutcome.SINGLE_PART_UPLOAD,
        duration_seconds=0.5,
    )
    recorder.record_upload(
        operation=SmallFileOperation.CREATE,
        outcome=SmallFileMetricOutcome.COMMITTED,
        duration_seconds=1.5,
    )
    recorder.record_replay(operation=SmallFileOperation.UPDATE)
    recorder.record_rejection(
        operation=SmallFileOperation.CREATE,
        reason_code=SmallFileRejectionReason.SMALL_FILE_SIZE_LIMIT_EXCEEDED,
    )
    assert (
        recorder.preflight_count(
            SmallFileOperation.CREATE, SmallFilePreflightOutcome.SINGLE_PART_UPLOAD
        )
        == 1
    )
    assert recorder.upload_count(SmallFileOperation.CREATE, SmallFileMetricOutcome.COMMITTED) == 1
    assert recorder.replay_count(SmallFileOperation.UPDATE) == 1
    assert (
        recorder.rejection_count(
            SmallFileOperation.CREATE, SmallFileRejectionReason.SMALL_FILE_SIZE_LIMIT_EXCEEDED
        )
        == 1
    )
    assert repr(recorder) == "InMemorySmallFileSyncMetrics(redacted)"


def test_in_memory_metrics_reject_open_text_labels_and_bad_durations() -> None:
    recorder = InMemorySmallFileSyncMetrics()
    with pytest.raises(ValueError, match="closed enum member"):
        recorder.record_preflight(
            operation="create",  # type: ignore[arg-type]
            outcome=SmallFilePreflightOutcome.EXCLUDED,
            duration_seconds=0.1,
        )
    with pytest.raises(ValueError, match="closed enum member"):
        recorder.record_upload(
            operation=SmallFileOperation.CREATE,
            outcome="committed",  # type: ignore[arg-type]
            duration_seconds=0.1,
        )
    with pytest.raises(ValueError, match="finite and non-negative"):
        recorder.record_preflight(
            operation=SmallFileOperation.CREATE,
            outcome=SmallFilePreflightOutcome.EXCLUDED,
            duration_seconds=-1.0,
        )
    with pytest.raises(ValueError, match="closed enum member"):
        recorder.record_rejection(
            operation=SmallFileOperation.CREATE,
            reason_code="small_file_size_limit_exceeded",  # type: ignore[arg-type]
        )
