"""Source conflict wire models: strict bodies, renderers and the choice matrix.

These tests pin the boundary conversions of the Conflict Inbox API (Child 8
spec 6): the strict resolve request whose closed grammar surfaces each
violation as the single closed ``source_conflict_input_invalid`` reason
token, the safe read-model renderers that project only opaque identifiers
and closed labels onto the wire, the resolution result renderer, and the
choices-by-kind/media-type matrix — including the byteless-candidate
exclusion that never offers an unappliable ``keep_local``/``save_merged``
choice. The small-file conflict-outcome rendering (the capture grant and
the replayed conflict identity the plugin needs to reach the captured
conflict) is pinned here as well.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from api_runtime.small_file_sync_models import small_file_preflight_data
from api_runtime.source_conflict_models import (
    MAX_CONFLICT_PAGE_LIMIT,
    SourceConflictDetailData,
    SourceConflictResolveRequest,
    allowed_resolution_choices,
    is_mergeable_conflict_media_type,
    source_conflict_data,
    source_conflict_detail_data,
    source_conflict_resolution_data,
    to_domain_resolve_command,
)
from pydantic import ValidationError

from personal_os.error_contracts.codes import ErrorCode
from personal_os.small_file_sync.contracts import (
    SmallFilePreflightOutcome,
    UploadOperationToken,
)
from personal_os.small_file_sync.service import SmallFilePreflightResult
from personal_os.source_conflicts.commands import ConflictResolutionResult
from personal_os.source_conflicts.contracts import (
    ConflictCandidate,
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
    ConflictStatus,
    SourceConflict,
)
from personal_os.source_conflicts.errors import SourceConflictError

_WORKSPACE_ID = uuid4()
_CAPTURED_AT = datetime(2026, 9, 2, 9, 15, 0, tzinfo=UTC)
_COMPLETED_AT = datetime(2026, 9, 2, 9, 40, 0, tzinfo=UTC)

#: The exact safe member set of one rendered conflict.
CONFLICT_DATA_MEMBERS: frozenset[str] = frozenset(
    {
        "conflict_id",
        "source_id",
        "conflict_kind",
        "status",
        "originating_event_id",
        "originating_device_id",
        "base_version_id",
        "observed_remote_version_id",
        "candidate_kind",
        "verified_candidate_object_id",
        "captured_at",
        "resolution_kind",
        "resolution_event_id",
        "resulting_version_id",
        "successor_conflict_id",
        "closed_at",
    }
)

#: Substrings no rendered member may ever carry.
FORBIDDEN_RENDER_MARKERS: tuple[str, ...] = (
    "object_key",
    "receipt",
    "presign",
    "bucket",
    "provider",
    "secret",
)


def _conflict(**overrides: Any) -> SourceConflict:
    fields: dict[str, Any] = dict(
        conflict_id=uuid4(),
        workspace_id=_WORKSPACE_ID,
        source_id=uuid4(),
        conflict_kind=ConflictKind.STALE_CONTENT,
        status=ConflictStatus.OPEN,
        originating_event_id=uuid4(),
        originating_device_id=uuid4(),
        base_version_id=uuid4(),
        observed_remote_version_id=uuid4(),
        candidate=ConflictCandidate.content(uuid4()),
        captured_at=_CAPTURED_AT,
        resolution_kind=None,
        resolution_event_id=None,
        resulting_version_id=None,
        successor_conflict_id=None,
        closed_at=None,
    )
    fields.update(overrides)
    return SourceConflict(**fields)


def _resolve_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "resolution_event_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "resolution_kind": "keep_remote",
        "reviewed_remote_version_id": str(uuid4()),
        "verified_candidate_object_id": None,
    }
    body.update(overrides)
    return body


# --- the strict resolve request ---------------------------------------------------------------


def test_resolve_request_rejects_extra_members_including_raw_merged_bytes() -> None:
    for extra in ({"raw": "secret"}, {"merged_bytes": "b64"}, {"content": "text"}):
        with pytest.raises(ValidationError):
            SourceConflictResolveRequest.model_validate({**_resolve_body(), **extra})


def test_resolve_request_requires_exactly_its_closed_members() -> None:
    body = SourceConflictResolveRequest.model_validate(_resolve_body())
    assert set(body.model_dump()) == {
        "resolution_event_id",
        "idempotency_key",
        "resolution_kind",
        "reviewed_remote_version_id",
        "verified_candidate_object_id",
    }
    with pytest.raises(ValidationError):
        SourceConflictResolveRequest.model_validate(_resolve_body(resolution_kind="unknown"))


def test_to_domain_resolve_command_converts_each_closed_shape() -> None:
    keep_remote = to_domain_resolve_command(
        SourceConflictResolveRequest.model_validate(_resolve_body()), conflict_id=uuid4()
    )
    assert keep_remote.resolution_kind is ConflictResolutionKind.KEEP_REMOTE

    save_merged = to_domain_resolve_command(
        SourceConflictResolveRequest.model_validate(
            _resolve_body(resolution_kind="save_merged", verified_candidate_object_id=str(uuid4()))
        ),
        conflict_id=uuid4(),
    )
    assert save_merged.verified_candidate_object_id is not None

    keep_local_without_remote = to_domain_resolve_command(
        SourceConflictResolveRequest.model_validate(
            _resolve_body(resolution_kind="keep_local", reviewed_remote_version_id=None)
        ),
        conflict_id=uuid4(),
    )
    assert keep_local_without_remote.reviewed_remote_version_id is None
    assert keep_local_without_remote.verified_candidate_object_id is None


def test_to_domain_resolve_command_maps_each_violation_to_its_closed_reason() -> None:
    cases: list[tuple[dict[str, Any], str]] = [
        (_resolve_body(resolution_event_id=str(UUID(int=0))), "resolution_event_id_invalid"),
        (_resolve_body(idempotency_key=str(UUID(int=0))), "idempotency_key_invalid"),
        (_resolve_body(reviewed_remote_version_id=str(UUID(int=0))), "reviewed_remote_invalid"),
        (
            _resolve_body(resolution_kind="save_merged", verified_candidate_object_id=None),
            "candidate_object_invalid",
        ),
        (
            _resolve_body(resolution_kind="keep_local", verified_candidate_object_id=str(uuid4())),
            "candidate_object_invalid",
        ),
        (_resolve_body(verified_candidate_object_id=str(UUID(int=0))), "candidate_object_invalid"),
    ]
    for body, reason in cases:
        with pytest.raises(SourceConflictError) as raised:
            to_domain_resolve_command(
                SourceConflictResolveRequest.model_validate(body), conflict_id=uuid4()
            )
        assert raised.value.error_code is ErrorCode.SOURCE_CONFLICT_INPUT_INVALID
        assert raised.value.safe_details["reason"].value == reason, body


def test_to_domain_resolve_command_rejects_the_nil_conflict_identity() -> None:
    with pytest.raises(SourceConflictError) as raised:
        to_domain_resolve_command(
            SourceConflictResolveRequest.model_validate(_resolve_body()),
            conflict_id=UUID(int=0),
        )
    assert raised.value.error_code is ErrorCode.SOURCE_CONFLICT_INPUT_INVALID


# --- safe read-model renderers ------------------------------------------------------------------


def test_source_conflict_data_renders_exactly_the_safe_members() -> None:
    conflict = _conflict()
    rendered = source_conflict_data(conflict).model_dump(mode="json")
    assert set(rendered) == CONFLICT_DATA_MEMBERS
    assert rendered["conflict_id"] == str(conflict.conflict_id)
    assert rendered["conflict_kind"] == "stale_content"
    assert rendered["status"] == "open"
    assert rendered["candidate_kind"] == "content"
    assert rendered["captured_at"] == "2026-09-02T09:15:00Z"
    assert "workspace_id" not in rendered
    for marker in FORBIDDEN_RENDER_MARKERS:
        assert marker not in rendered


def test_source_conflict_data_renders_the_byteless_candidate_shape() -> None:
    conflict = _conflict(
        conflict_kind=ConflictKind.DELETE_REMOTE_EDIT,
        candidate=ConflictCandidate.delete(),
    )
    rendered = source_conflict_data(conflict).model_dump(mode="json")
    assert rendered["candidate_kind"] == "delete"
    assert rendered["verified_candidate_object_id"] is None


def test_source_conflict_data_renders_the_terminal_status_shapes() -> None:
    resolved = _conflict(
        status=ConflictStatus.RESOLVED,
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
        resolution_event_id=uuid4(),
        resulting_version_id=uuid4(),
        closed_at=_COMPLETED_AT,
    )
    rendered = source_conflict_data(resolved).model_dump(mode="json")
    assert rendered["status"] == "resolved"
    assert rendered["resolution_kind"] == "keep_local"
    assert rendered["resulting_version_id"] is not None

    superseded = _conflict(
        status=ConflictStatus.SUPERSEDED,
        resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
        resolution_event_id=uuid4(),
        successor_conflict_id=uuid4(),
        closed_at=_COMPLETED_AT,
    )
    superseded_rendered = source_conflict_data(superseded).model_dump(mode="json")
    assert superseded_rendered["successor_conflict_id"] is not None
    assert superseded_rendered["resulting_version_id"] is None


def test_source_conflict_detail_data_extends_the_safe_members_with_choices() -> None:
    conflict = _conflict()
    detail = source_conflict_detail_data(
        conflict, choices=(ConflictResolutionKind.KEEP_REMOTE, ConflictResolutionKind.KEEP_LOCAL)
    )
    assert isinstance(detail, SourceConflictDetailData)
    rendered = detail.model_dump(mode="json")
    assert set(rendered) == CONFLICT_DATA_MEMBERS | {"choices"}
    assert rendered["choices"] == ["keep_remote", "keep_local"]


# --- the choices-by-kind/media-type matrix ------------------------------------------------------


def test_an_open_content_conflict_offers_the_text_merge_choices_for_markdown() -> None:
    conflict = _conflict()
    assert allowed_resolution_choices(conflict, candidate_media_type="text/markdown") == (
        ConflictResolutionKind.KEEP_REMOTE,
        ConflictResolutionKind.KEEP_LOCAL,
        ConflictResolutionKind.SAVE_MERGED,
    )


def test_an_open_content_conflict_offers_no_merge_choice_for_binary_media() -> None:
    conflict = _conflict()
    assert allowed_resolution_choices(
        conflict, candidate_media_type="application/octet-stream"
    ) == (ConflictResolutionKind.KEEP_REMOTE, ConflictResolutionKind.KEEP_LOCAL)


def test_an_open_content_conflict_without_a_resolvable_media_type_fails_closed() -> None:
    conflict = _conflict()
    assert allowed_resolution_choices(conflict, candidate_media_type=None) == (
        ConflictResolutionKind.KEEP_REMOTE,
        ConflictResolutionKind.KEEP_LOCAL,
    )


def test_a_byteless_conflict_offers_only_keep_remote() -> None:
    delete_remote_edit = _conflict(
        conflict_kind=ConflictKind.DELETE_REMOTE_EDIT,
        candidate=ConflictCandidate.delete(),
    )
    choices = allowed_resolution_choices(delete_remote_edit, candidate_media_type=None)
    assert choices == (ConflictResolutionKind.KEEP_REMOTE,)
    assert ConflictResolutionKind.KEEP_LOCAL not in choices
    assert ConflictResolutionKind.SAVE_MERGED not in choices

    # A byteless locator collision (no identified canonical source) is the
    # same shape: only keep_remote applies.
    byteless_collision = _conflict(
        conflict_kind=ConflictKind.LOCATOR_COLLISION,
        source_id=None,
        candidate=ConflictCandidate.delete(),
        observed_remote_version_id=None,
    )
    collision_choices = allowed_resolution_choices(byteless_collision, candidate_media_type=None)
    assert collision_choices == (ConflictResolutionKind.KEEP_REMOTE,)


def test_a_terminal_conflict_offers_no_choice_at_all() -> None:
    resolved = _conflict(
        status=ConflictStatus.RESOLVED,
        resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
        resolution_event_id=uuid4(),
        closed_at=_COMPLETED_AT,
    )
    assert allowed_resolution_choices(resolved, candidate_media_type="text/markdown") == ()


def test_is_mergeable_conflict_media_type_covers_exactly_the_spec_named_text_form() -> None:
    assert is_mergeable_conflict_media_type("text/markdown") is True
    assert is_mergeable_conflict_media_type("text/plain") is False
    assert is_mergeable_conflict_media_type("application/pdf") is False
    assert is_mergeable_conflict_media_type("image/png") is False


def test_the_page_limit_bound_mirrors_the_store_bound() -> None:
    assert MAX_CONFLICT_PAGE_LIMIT == 200


# --- the resolution result renderer --------------------------------------------------------------


def test_source_conflict_resolution_data_renders_both_typed_outcomes() -> None:
    resolved = ConflictResolutionResult(
        kind=ConflictResolutionOutcome.RESOLVED,
        conflict_id=uuid4(),
        resolution_event_id=uuid4(),
        resolution_kind=ConflictResolutionKind.SAVE_MERGED,
        resulting_version_id=uuid4(),
        successor=None,
        completed_at=_COMPLETED_AT,
    )
    rendered = source_conflict_resolution_data(resolved).model_dump(mode="json")
    assert set(rendered) == {
        "outcome",
        "conflict_id",
        "resolution_event_id",
        "resolution_kind",
        "resulting_version_id",
        "successor_conflict_id",
        "completed_at",
    }
    assert rendered["outcome"] == "resolved"
    assert rendered["resulting_version_id"] is not None
    assert rendered["successor_conflict_id"] is None

    successor = _conflict(observed_remote_version_id=uuid4())
    stale = ConflictResolutionResult(
        kind=ConflictResolutionOutcome.STALE_SUCCESSOR,
        conflict_id=uuid4(),
        resolution_event_id=uuid4(),
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
        resulting_version_id=None,
        successor=successor,
        completed_at=_COMPLETED_AT,
    )
    stale_rendered = source_conflict_resolution_data(stale).model_dump(mode="json")
    assert stale_rendered["outcome"] == "stale_successor"
    assert stale_rendered["resulting_version_id"] is None
    assert stale_rendered["successor_conflict_id"] == str(successor.conflict_id)


# --- the small-file conflict-outcome rendering (capture grant + replay identity) ------------------


def test_small_file_conflict_preflight_data_renders_the_capture_grant() -> None:
    result = SmallFilePreflightResult(
        outcome=SmallFilePreflightOutcome.CONFLICT,
        operation_token=UploadOperationToken("a" * 43),
        expires_at=datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC),
    )
    rendered = small_file_preflight_data(result).model_dump(mode="json", exclude_unset=True)
    assert set(rendered) == {"outcome", "operation_id", "expires_at"}
    assert rendered["operation_id"] == "a" * 43
    assert rendered["outcome"] == "conflict"


def test_small_file_conflict_preflight_data_renders_the_replayed_conflict_identity() -> None:
    conflict_id = uuid4()
    result = SmallFilePreflightResult(
        outcome=SmallFilePreflightOutcome.CONFLICT,
        conflict_id=conflict_id,
    )
    rendered = small_file_preflight_data(result).model_dump(mode="json", exclude_unset=True)
    assert set(rendered) == {"outcome", "conflict_id"}
    assert rendered["conflict_id"] == str(conflict_id)


def test_small_file_conflict_preflight_data_renders_the_bare_conflict_verdict() -> None:
    result = SmallFilePreflightResult(outcome=SmallFilePreflightOutcome.CONFLICT)
    rendered = small_file_preflight_data(result).model_dump(mode="json", exclude_unset=True)
    assert rendered == {"outcome": "conflict"}
