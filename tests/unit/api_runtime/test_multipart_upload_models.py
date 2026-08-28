"""Strict multipart upload wire models and the domain boundary conversion.

These tests pin the closed wire grammar of the five multipart session
endpoints (Child 7 spec 5): the create body mirrors the journal preflight
shape — never a workspace, device, user or storage selector — and validates
the server-owned multipart routing range at the boundary; every response
model is frozen and closed for extra fields; the one part-URL response is
the sole model carrying a ``url`` member, and no plan, status or completion
model ever admits one, so a signed URL cannot leak into any other surface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Final
from uuid import uuid4

import pytest
from api_runtime.multipart_upload_models import (
    MultipartCompletionData,
    MultipartPartUrlData,
    MultipartSessionCreateRequest,
    MultipartSessionPlanData,
    MultipartSessionStatusData,
    multipart_completion_data,
    multipart_part_url_data,
    multipart_session_plan_data,
    multipart_session_status_data,
    to_multipart_session_preflight,
)
from pydantic import ValidationError

from personal_os.error_contracts.codes import ErrorCode
from personal_os.multipart_upload.contracts import (
    MultipartCompletionResult,
    MultipartPartRange,
    MultipartPartUrl,
    MultipartSessionState,
    MultipartSessionStatus,
    MultipartUploadPlan,
    MultipartUploadSessionId,
)
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.small_file_sync.contracts import (
    MAX_SINGLE_PART_FILE_SIZE_BYTES,
    MAX_UPLOAD_FILE_SIZE_BYTES,
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
)
from personal_os.small_file_sync.errors import SmallFileSyncError

#: A deterministic 20 MiB preimage: three parts (8 MiB, 8 MiB, 4 MiB).
_MULTIPART_SIZE_BYTES: Final[int] = 20 * 1024 * 1024
_PREIMAGE: Final[bytes] = b"\x00" * _MULTIPART_SIZE_BYTES
_PREIMAGE_SHA256: Final[str] = sha256(_PREIMAGE).hexdigest()
_EXPIRES_AT: Final[datetime] = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)


def _create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "event_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "operation": "create",
        "local_file_id": str(uuid4()),
        "source_id": None,
        "base_version_id": None,
        "normalized_locator": "notes/large-note.md",
        "sha256": _PREIMAGE_SHA256,
        "size_bytes": _MULTIPART_SIZE_BYTES,
        "media_type": "text/markdown",
        "policy_revision": 7,
    }
    body.update(overrides)
    return body


def _terminal() -> SmallFileTerminalResult:
    return SmallFileTerminalResult(
        result_kind=SmallFileTerminalResultKind.COMMITTED,
        source_id=uuid4(),
        source_version_id=uuid4(),
        content_version=1,
        committed_at=_EXPIRES_AT,
    )


def _session_id() -> MultipartUploadSessionId:
    return MultipartUploadSessionId("s" * 40)


# --- the strict create body ------------------------------------------------------


def test_create_request_is_frozen_and_forbids_extra_fields() -> None:
    request = MultipartSessionCreateRequest(**_create_body())
    with pytest.raises(ValidationError):
        request.unexpected_member = "x"  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        MultipartSessionCreateRequest(**_create_body(workspace_id=str(uuid4())))
    with pytest.raises(ValidationError):
        MultipartSessionCreateRequest(**_create_body(device_id=str(uuid4())))
    with pytest.raises(ValidationError):
        MultipartSessionCreateRequest(**_create_body(presigned_url="https://host.invalid/p"))
    with pytest.raises(ValidationError):
        MultipartSessionCreateRequest(
            **{name: value for name, value in _create_body().items() if name != "size_bytes"}
        )


def test_create_request_accepts_exactly_the_preflight_mirror_fields() -> None:
    request = MultipartSessionCreateRequest(**_create_body())
    assert set(request.model_dump()) == {
        "event_id",
        "idempotency_key",
        "operation",
        "local_file_id",
        "source_id",
        "base_version_id",
        "normalized_locator",
        "sha256",
        "size_bytes",
        "media_type",
        "policy_revision",
    }


def test_boundary_conversion_rejects_sizes_outside_the_multipart_routing_range() -> None:
    with pytest.raises(MultipartUploadError) as single_part:
        to_multipart_session_preflight(
            MultipartSessionCreateRequest(
                **_create_body(size_bytes=MAX_SINGLE_PART_FILE_SIZE_BYTES)
            )
        )
    assert single_part.value.error_code is ErrorCode.MULTIPART_PART_INVALID
    with pytest.raises(MultipartUploadError) as single_part_over:
        to_multipart_session_preflight(
            MultipartSessionCreateRequest(
                **_create_body(size_bytes=MAX_SINGLE_PART_FILE_SIZE_BYTES - 1)
            )
        )
    assert single_part_over.value.error_code is ErrorCode.MULTIPART_PART_INVALID


def test_boundary_conversion_maps_the_product_maximum_rejection() -> None:
    over_maximum = _create_body(size_bytes=MAX_UPLOAD_FILE_SIZE_BYTES + 1, sha256="0" * 64)
    with pytest.raises(SmallFileSyncError) as rejected:
        to_multipart_session_preflight(MultipartSessionCreateRequest(**over_maximum))
    assert rejected.value.error_code is ErrorCode.SMALL_FILE_SIZE_LIMIT_EXCEEDED


def test_boundary_conversion_maps_domain_grammar_violations() -> None:
    with pytest.raises(SmallFileSyncError) as invalid:
        to_multipart_session_preflight(
            MultipartSessionCreateRequest(**_create_body(media_type="text/markdown; charset=utf-8"))
        )
    assert invalid.value.error_code is ErrorCode.SMALL_FILE_PREFLIGHT_INVALID
    with pytest.raises(SmallFileSyncError) as locator_invalid:
        to_multipart_session_preflight(
            MultipartSessionCreateRequest(**_create_body(normalized_locator="notes\\file.md"))
        )
    assert locator_invalid.value.error_code is ErrorCode.SMALL_FILE_PREFLIGHT_INVALID


def test_boundary_conversion_produces_the_locator_bound_domain_preflight() -> None:
    preflight = to_multipart_session_preflight(MultipartSessionCreateRequest(**_create_body()))
    assert preflight.size_bytes == _MULTIPART_SIZE_BYTES
    assert preflight.normalized_locator.value == "notes/large-note.md"
    assert preflight.policy_revision_number == 7


# --- the plan renderer ------------------------------------------------------------


def test_plan_data_carries_exactly_the_safe_geometry_members() -> None:
    plan = MultipartUploadPlan(
        session_id=_session_id(),
        part_size_bytes=8 * 1024 * 1024,
        part_count=3,
        expires_at=_EXPIRES_AT + timedelta(hours=24),
    )
    data = multipart_session_plan_data(plan)
    assert isinstance(data, MultipartSessionPlanData)
    assert set(data.model_dump()) == {
        "session_id",
        "part_size_bytes",
        "part_count",
        "expires_at",
    }
    assert data.session_id == plan.session_id.value
    assert data.part_count == 3
    assert "url" not in data.model_dump()


# --- the status renderer ----------------------------------------------------------


def _status(
    *,
    state: MultipartSessionState = MultipartSessionState.UPLOADING,
    terminal: SmallFileTerminalResult | None = None,
) -> MultipartSessionStatus:
    return MultipartSessionStatus(
        session_id=_session_id(),
        state=state,
        part_size_bytes=8 * 1024 * 1024,
        part_count=3,
        expires_at=_EXPIRES_AT + timedelta(hours=24),
        completed_part_numbers=frozenset({2, 1}),
        terminal_result=terminal,
    )


def test_status_data_orders_completed_part_numbers_and_admits_no_url() -> None:
    data = multipart_session_status_data(_status())
    assert isinstance(data, MultipartSessionStatusData)
    assert data.completed_part_numbers == (1, 2)
    assert data.terminal_result is None
    rendered = data.model_dump(mode="json", exclude_unset=True)
    assert "url" not in rendered
    assert rendered["state"] == "uploading"


def test_status_data_carries_the_terminal_receipt_only_once_committed() -> None:
    data = multipart_session_status_data(
        _status(state=MultipartSessionState.COMMITTED, terminal=_terminal())
    )
    assert data.terminal_result is not None
    assert data.terminal_result.result_kind is SmallFileTerminalResultKind.COMMITTED


# --- the one URL response ---------------------------------------------------------


def test_part_url_data_is_the_sole_model_with_a_url_member() -> None:
    part_url = MultipartPartUrl(
        part_number=3,
        byte_range=MultipartPartRange(
            part_number=3, offset_bytes=2 * 8 * 1024 * 1024, size_bytes=4 * 1024 * 1024
        ),
        url="https://staging.example.invalid/signed-part",
        expires_at=_EXPIRES_AT + timedelta(minutes=10),
    )
    data = multipart_part_url_data(part_url)
    assert isinstance(data, MultipartPartUrlData)
    assert set(data.model_dump()) == {
        "part_number",
        "offset_bytes",
        "size_bytes",
        "url",
        "expires_at",
    }
    assert data.url.startswith("https://")
    assert data.size_bytes == 4 * 1024 * 1024
    for model in (
        MultipartSessionPlanData,
        MultipartSessionStatusData,
        MultipartCompletionData,
    ):
        assert "url" not in model.model_fields


def test_part_url_model_is_frozen_and_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MultipartPartUrlData.model_validate(
            {
                "part_number": 1,
                "offset_bytes": 0,
                "size_bytes": 8,
                "url": "https://staging.example.invalid/signed-part",
                "expires_at": _EXPIRES_AT,
                "provider_upload_id": "upload-1",
            }
        )


# --- the completion renderer ------------------------------------------------------


def test_completion_data_carries_the_frozen_terminal_only_in_the_committed_state() -> None:
    committed = multipart_completion_data(
        MultipartCompletionResult(
            state=MultipartSessionState.COMMITTED, terminal_result=_terminal()
        )
    )
    assert isinstance(committed, MultipartCompletionData)
    assert committed.terminal_result is not None
    assert "url" not in committed.model_dump()
