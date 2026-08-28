"""Closed multipart upload contracts: geometry, redaction and state machine.

Asserts the immutable transfer geometry of the Child 7 spec (an 8 MiB ordinary
part, a positive final part of at most 8 MiB, at most 13 parts for
16 MiB < size <= 100 MiB), the opaque public session-ID grammar, the redacted
``repr`` of every sensitive value (session ID, presigned part URL and the
provider identity values private to the ports), the server session state
machine of spec 4.2 with its closed transition table, the 24-hour session
expiry derivation and the safe status/completion result shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from personal_os.multipart_upload.contracts import (
    MAX_MULTIPART_PART_COUNT,
    MULTIPART_PART_SIZE_BYTES,
    MULTIPART_PART_URL_LIFETIME,
    MULTIPART_SESSION_LIFETIME,
    MultipartCompletionResult,
    MultipartPartGeometry,
    MultipartPartRange,
    MultipartPartUrl,
    MultipartSessionState,
    MultipartSessionStatus,
    MultipartUploadPlan,
    MultipartUploadSessionId,
    compute_multipart_session_expiry,
)
from personal_os.multipart_upload.ports import (
    MultipartProviderPartETag,
    MultipartProviderUploadId,
    MultipartUploadApplicationService,
)
from personal_os.small_file_sync.contracts import (
    MAX_SINGLE_PART_FILE_SIZE_BYTES,
    MAX_UPLOAD_FILE_SIZE_BYTES,
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
)

_SESSION_ID_TEXT = "session-value" * 4
_EXPIRES_AT = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)


def _session_id() -> MultipartUploadSessionId:
    return MultipartUploadSessionId(_SESSION_ID_TEXT)


def _terminal_result() -> SmallFileTerminalResult:
    return SmallFileTerminalResult(
        result_kind=SmallFileTerminalResultKind.COMMITTED,
        source_id=uuid4(),
        source_version_id=uuid4(),
        content_version=2,
        committed_at=datetime(2026, 8, 28, 9, 0, 0, tzinfo=UTC),
    )


# --- immutable transfer geometry (spec 4) ----------------------------------------------------


def test_plan_for_maximum_file_has_thirteen_exact_parts() -> None:
    plan = MultipartUploadPlan.from_size_bytes(100 * 1024 * 1024)
    assert plan.part_count == 13
    assert plan.part_range(13).size_bytes == 4 * 1024 * 1024


def test_geometry_constants_pin_the_spec_transfer_shape() -> None:
    assert MULTIPART_PART_SIZE_BYTES == 8 * 1024 * 1024
    assert MAX_MULTIPART_PART_COUNT == 13
    assert timedelta(hours=24) == MULTIPART_SESSION_LIFETIME
    assert timedelta(minutes=10) == MULTIPART_PART_URL_LIFETIME
    assert MAX_UPLOAD_FILE_SIZE_BYTES == 100 * 1024 * 1024


def test_minimum_routable_file_has_three_parts_with_a_one_byte_final_part() -> None:
    geometry = MultipartPartGeometry.from_size_bytes(MAX_SINGLE_PART_FILE_SIZE_BYTES + 1)

    assert geometry.total_size_bytes == MAX_SINGLE_PART_FILE_SIZE_BYTES + 1
    assert geometry.part_count == 3
    assert geometry.part_range(1).size_bytes == MULTIPART_PART_SIZE_BYTES
    assert geometry.part_range(2).size_bytes == MULTIPART_PART_SIZE_BYTES
    assert geometry.part_range(2).offset_bytes == MULTIPART_PART_SIZE_BYTES
    assert geometry.part_range(3).size_bytes == 1
    assert geometry.part_range(3).offset_bytes == 2 * MULTIPART_PART_SIZE_BYTES


def test_exact_part_size_multiple_ends_on_a_full_final_part() -> None:
    geometry = MultipartPartGeometry.from_size_bytes(3 * MULTIPART_PART_SIZE_BYTES)

    assert geometry.part_count == 3
    final_range = geometry.part_range(3)
    assert final_range.size_bytes == MULTIPART_PART_SIZE_BYTES
    assert final_range.offset_bytes == 2 * MULTIPART_PART_SIZE_BYTES


@pytest.mark.parametrize(
    "size_bytes",
    [
        -1,
        0,
        MAX_SINGLE_PART_FILE_SIZE_BYTES,
        MAX_SINGLE_PART_FILE_SIZE_BYTES - 1,
        MAX_UPLOAD_FILE_SIZE_BYTES + 1,
    ],
)
def test_geometry_rejects_sizes_outside_the_routing_range(size_bytes: int) -> None:
    with pytest.raises(ValueError, match="routing range"):
        MultipartPartGeometry.from_size_bytes(size_bytes)


def test_part_range_rejects_out_of_bounds_part_numbers() -> None:
    geometry = MultipartPartGeometry.from_size_bytes(MAX_UPLOAD_FILE_SIZE_BYTES)

    with pytest.raises(ValueError, match="part number"):
        geometry.part_range(0)
    with pytest.raises(ValueError, match="part number"):
        geometry.part_range(MAX_MULTIPART_PART_COUNT + 1)


def test_geometry_rejects_a_drifted_part_count() -> None:
    with pytest.raises(ValueError, match="part_count"):
        MultipartPartGeometry(
            total_size_bytes=MAX_UPLOAD_FILE_SIZE_BYTES,
            part_size_bytes=MULTIPART_PART_SIZE_BYTES,
            part_count=MAX_MULTIPART_PART_COUNT - 1,
        )


def test_geometry_rejects_a_non_ordinary_part_size() -> None:
    with pytest.raises(ValueError, match="part_size_bytes"):
        MultipartPartGeometry(
            total_size_bytes=3 * MULTIPART_PART_SIZE_BYTES,
            part_size_bytes=4 * 1024 * 1024,
            part_count=6,
        )


def test_part_range_is_a_bounded_positive_byte_window() -> None:
    assert MultipartPartRange(part_number=1, offset_bytes=0, size_bytes=1).size_bytes == 1
    with pytest.raises(ValueError, match="positive part number"):
        MultipartPartRange(part_number=0, offset_bytes=0, size_bytes=1)
    with pytest.raises(ValueError, match="non-negative byte offset"):
        MultipartPartRange(part_number=1, offset_bytes=-1, size_bytes=1)
    with pytest.raises(ValueError, match="1 to"):
        MultipartPartRange(part_number=1, offset_bytes=0, size_bytes=0)
    with pytest.raises(ValueError, match="1 to"):
        MultipartPartRange(part_number=1, offset_bytes=0, size_bytes=MULTIPART_PART_SIZE_BYTES + 1)


# --- opaque public session ID -----------------------------------------------------------------


def test_session_id_and_provider_values_redact_repr() -> None:
    assert "session-value" not in repr(MultipartUploadSessionId("session-value" * 4))


def test_provider_identity_values_redact_repr() -> None:
    upload_id = MultipartProviderUploadId("provider-upload-id-sentinel-0123456789abcdef")
    etag = MultipartProviderPartETag('"provider-etag-sentinel-0123456789abcdef"')

    rendered = f"{upload_id!r} {etag!r}"
    assert "provider-upload-id-sentinel" not in rendered
    assert "provider-etag-sentinel" not in rendered
    assert upload_id.value == "provider-upload-id-sentinel-0123456789abcdef"
    assert etag.value == '"provider-etag-sentinel-0123456789abcdef"'


def test_session_id_accepts_opaque_url_safe_grammar() -> None:
    session_id = _session_id()

    assert session_id.value == _SESSION_ID_TEXT
    assert session_id == _session_id()


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("a" * 31, "32 to 128"),
        ("a" * 129, "32 to 128"),
        ("Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1=", "URL-safe"),
        ("Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1+", "URL-safe"),
        ("Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1/", "URL-safe"),
        ("Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1 ", "printable"),
        ("Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1\n", "printable"),
    ],
)
def test_session_id_rejects_out_of_grammar_text(value: str, pattern: str) -> None:
    with pytest.raises(ValueError, match=pattern):
        MultipartUploadSessionId(value)


def test_session_id_rejects_raw_uuid_text() -> None:
    with pytest.raises(ValueError, match="URL-safe"):
        MultipartUploadSessionId(str(uuid4()))


# --- session-bound upload plan ------------------------------------------------------------------


def test_upload_plan_binds_session_geometry_and_expiry() -> None:
    plan = MultipartUploadPlan(
        session_id=_session_id(),
        part_size_bytes=MULTIPART_PART_SIZE_BYTES,
        part_count=MAX_MULTIPART_PART_COUNT,
        expires_at=_EXPIRES_AT,
    )

    assert plan.session_id == _session_id()
    assert plan.part_size_bytes == MULTIPART_PART_SIZE_BYTES
    assert plan.part_count == MAX_MULTIPART_PART_COUNT
    assert plan.expires_at == _EXPIRES_AT


def test_upload_plan_rejects_drifted_geometry_and_naive_expiry() -> None:
    with pytest.raises(ValueError, match="part_size_bytes"):
        MultipartUploadPlan(
            session_id=_session_id(),
            part_size_bytes=4 * 1024 * 1024,
            part_count=MAX_MULTIPART_PART_COUNT,
            expires_at=_EXPIRES_AT,
        )
    with pytest.raises(ValueError, match="part_count"):
        MultipartUploadPlan(
            session_id=_session_id(),
            part_size_bytes=MULTIPART_PART_SIZE_BYTES,
            part_count=MAX_MULTIPART_PART_COUNT + 1,
            expires_at=_EXPIRES_AT,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        MultipartUploadPlan(
            session_id=_session_id(),
            part_size_bytes=MULTIPART_PART_SIZE_BYTES,
            part_count=MAX_MULTIPART_PART_COUNT,
            expires_at=datetime(2026, 8, 28, 12, 0, 0),
        )


def test_plan_repr_never_renders_the_session_id_value() -> None:
    plan = MultipartUploadPlan(
        session_id=_session_id(),
        part_size_bytes=MULTIPART_PART_SIZE_BYTES,
        part_count=3,
        expires_at=_EXPIRES_AT,
    )

    assert _SESSION_ID_TEXT not in repr(plan)


def test_session_expiry_is_exactly_twenty_four_hours_after_creation() -> None:
    created_at = datetime(2026, 8, 28, 9, 0, 0, tzinfo=UTC)

    assert compute_multipart_session_expiry(created_at) == created_at + timedelta(hours=24)
    offset_zone = timezone(timedelta(hours=7))
    created_offset = datetime(2026, 8, 28, 16, 0, 0, tzinfo=offset_zone)
    assert compute_multipart_session_expiry(created_offset) == (
        created_offset.astimezone(UTC) + timedelta(hours=24)
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_multipart_session_expiry(datetime(2026, 8, 28, 9, 0, 0))


# --- server session state machine (spec 4.2) ---------------------------------------------------


def test_completed_session_cannot_transition_back_to_uploading() -> None:
    with pytest.raises(ValueError):
        MultipartSessionState.COMMITTED.require_transition_to(MultipartSessionState.UPLOADING)


def test_session_state_values_match_the_spec_state_machine() -> None:
    assert {state.value for state in MultipartSessionState} == {
        "created",
        "uploading",
        "completing",
        "verifying",
        "promoting",
        "committed",
        "cancelling",
        "expired",
        "integrity_failed",
        "policy_denied",
        "cleanup_pending",
        "cleaned",
    }


def test_state_transition_table_matches_the_spec_state_machine() -> None:
    active_states = (
        MultipartSessionState.CREATED,
        MultipartSessionState.UPLOADING,
        MultipartSessionState.COMPLETING,
        MultipartSessionState.VERIFYING,
        MultipartSessionState.PROMOTING,
    )
    failure_exits = frozenset(
        {
            MultipartSessionState.CANCELLING,
            MultipartSessionState.EXPIRED,
            MultipartSessionState.INTEGRITY_FAILED,
            MultipartSessionState.POLICY_DENIED,
        }
    )
    # The forward chain advances exactly one step at a time.
    forward_chain = [
        (MultipartSessionState.CREATED, MultipartSessionState.UPLOADING),
        (MultipartSessionState.UPLOADING, MultipartSessionState.COMPLETING),
        (MultipartSessionState.COMPLETING, MultipartSessionState.VERIFYING),
        (MultipartSessionState.VERIFYING, MultipartSessionState.PROMOTING),
        (MultipartSessionState.PROMOTING, MultipartSessionState.COMMITTED),
    ]
    for source, target in forward_chain:
        assert source.allows_transition_to(target)
    # Skipping a forward step is refused.
    assert not MultipartSessionState.CREATED.allows_transition_to(MultipartSessionState.VERIFYING)
    # Every active state can exit to each terminal failure obligation.
    for state in active_states:
        for failure in failure_exits:
            assert state.allows_transition_to(failure), (state, failure)
    # Each failure obligation resolves into cleanup, then cleanliness.
    for failure in failure_exits:
        assert failure.allows_transition_to(MultipartSessionState.CLEANUP_PENDING)
    assert MultipartSessionState.CLEANUP_PENDING.allows_transition_to(MultipartSessionState.CLEANED)
    # The two terminal outcomes accept no further transition at all.
    for terminal in (MultipartSessionState.COMMITTED, MultipartSessionState.CLEANED):
        for target in MultipartSessionState:
            assert not terminal.allows_transition_to(target), (terminal, target)


def test_require_transition_to_accepts_a_closed_transition() -> None:
    MultipartSessionState.CREATED.require_transition_to(MultipartSessionState.UPLOADING)
    MultipartSessionState.CLEANUP_PENDING.require_transition_to(MultipartSessionState.CLEANED)


# --- signed part-URL envelope --------------------------------------------------------------------


def test_part_url_binds_one_exact_range_and_redacts_the_signed_url() -> None:
    geometry = MultipartPartGeometry.from_size_bytes(MAX_UPLOAD_FILE_SIZE_BYTES)
    signed_url = "https://storage.example.com/staging?X-Amz-Signature=secret-sentinel"

    part_url = MultipartPartUrl(
        part_number=MAX_MULTIPART_PART_COUNT,
        byte_range=geometry.part_range(MAX_MULTIPART_PART_COUNT),
        url=signed_url,
        expires_at=_EXPIRES_AT,
    )

    assert part_url.part_number == MAX_MULTIPART_PART_COUNT
    assert part_url.byte_range == geometry.part_range(MAX_MULTIPART_PART_COUNT)
    rendered = repr(part_url)
    assert "secret-sentinel" not in rendered
    assert "storage.example.com" not in rendered


def test_part_url_rejects_a_mismatched_range_and_naive_expiry() -> None:
    geometry = MultipartPartGeometry.from_size_bytes(MAX_UPLOAD_FILE_SIZE_BYTES)

    with pytest.raises(ValueError, match="part number"):
        MultipartPartUrl(
            part_number=1,
            byte_range=geometry.part_range(MAX_MULTIPART_PART_COUNT),
            url="https://storage.example.com/staging-part",
            expires_at=_EXPIRES_AT,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        MultipartPartUrl(
            part_number=1,
            byte_range=geometry.part_range(1),
            url="https://storage.example.com/staging-part",
            expires_at=datetime(2026, 8, 28, 12, 0, 0),
        )


def test_part_url_rejects_a_non_https_or_oversized_url() -> None:
    geometry = MultipartPartGeometry.from_size_bytes(MAX_UPLOAD_FILE_SIZE_BYTES)

    with pytest.raises(ValueError, match="https URL"):
        MultipartPartUrl(
            part_number=1,
            byte_range=geometry.part_range(1),
            url="http://storage.example.com/staging-part",
            expires_at=_EXPIRES_AT,
        )
    with pytest.raises(ValueError, match="https URL"):
        MultipartPartUrl(
            part_number=1,
            byte_range=geometry.part_range(1),
            url="not a url",
            expires_at=_EXPIRES_AT,
        )
    with pytest.raises(ValueError, match="https URL"):
        MultipartPartUrl(
            part_number=1,
            byte_range=geometry.part_range(1),
            url="https://storage.example.com/" + "a" * 8192,
            expires_at=_EXPIRES_AT,
        )


# --- safe status and completion results -----------------------------------------------------------


def test_session_status_reports_safe_progress_and_the_frozen_result() -> None:
    status = MultipartSessionStatus(
        session_id=_session_id(),
        state=MultipartSessionState.COMMITTED,
        part_size_bytes=MULTIPART_PART_SIZE_BYTES,
        part_count=MAX_MULTIPART_PART_COUNT,
        expires_at=_EXPIRES_AT,
        completed_part_numbers=frozenset(range(1, MAX_MULTIPART_PART_COUNT + 1)),
        terminal_result=_terminal_result(),
    )

    assert status.completed_part_numbers == frozenset(range(1, MAX_MULTIPART_PART_COUNT + 1))
    assert status.terminal_result is not None
    assert _SESSION_ID_TEXT not in repr(status)


def test_session_status_accepts_an_active_state_without_a_terminal_result() -> None:
    status = MultipartSessionStatus(
        session_id=_session_id(),
        state=MultipartSessionState.UPLOADING,
        part_size_bytes=MULTIPART_PART_SIZE_BYTES,
        part_count=3,
        expires_at=_EXPIRES_AT,
        completed_part_numbers=frozenset({1}),
        terminal_result=None,
    )

    assert status.terminal_result is None


def test_session_status_rejects_progress_outside_the_plan_geometry() -> None:
    with pytest.raises(ValueError, match="completed part numbers"):
        MultipartSessionStatus(
            session_id=_session_id(),
            state=MultipartSessionState.UPLOADING,
            part_size_bytes=MULTIPART_PART_SIZE_BYTES,
            part_count=3,
            expires_at=_EXPIRES_AT,
            completed_part_numbers=frozenset({4}),
            terminal_result=None,
        )


def test_session_status_ties_the_terminal_result_to_the_committed_state() -> None:
    # An active state carries no terminal result at all.
    with pytest.raises(ValueError, match="committed"):
        MultipartSessionStatus(
            session_id=_session_id(),
            state=MultipartSessionState.UPLOADING,
            part_size_bytes=MULTIPART_PART_SIZE_BYTES,
            part_count=3,
            expires_at=_EXPIRES_AT,
            completed_part_numbers=frozenset({1}),
            terminal_result=_terminal_result(),
        )
    # A committed session always carries its frozen terminal result.
    with pytest.raises(ValueError, match="committed"):
        MultipartSessionStatus(
            session_id=_session_id(),
            state=MultipartSessionState.COMMITTED,
            part_size_bytes=MULTIPART_PART_SIZE_BYTES,
            part_count=3,
            expires_at=_EXPIRES_AT,
            completed_part_numbers=frozenset({1, 2, 3}),
            terminal_result=None,
        )


def test_completion_result_returns_pending_state_or_the_frozen_commit() -> None:
    pending = MultipartCompletionResult(state=MultipartSessionState.VERIFYING, terminal_result=None)
    committed = MultipartCompletionResult(
        state=MultipartSessionState.COMMITTED, terminal_result=_terminal_result()
    )

    assert pending.terminal_result is None
    assert committed.terminal_result is not None


def test_completion_result_rejects_a_result_outside_the_committed_state() -> None:
    with pytest.raises(ValueError, match="committed"):
        MultipartCompletionResult(
            state=MultipartSessionState.UPLOADING, terminal_result=_terminal_result()
        )
    with pytest.raises(ValueError, match="committed"):
        MultipartCompletionResult(state=MultipartSessionState.COMMITTED, terminal_result=None)


# --- application service surface -----------------------------------------------------------------


def test_application_service_protocol_declares_the_closed_surface() -> None:
    for method_name in ("create_or_resume", "issue_part_url", "complete"):
        assert hasattr(MultipartUploadApplicationService, method_name)
