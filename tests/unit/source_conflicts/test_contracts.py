"""Closed source-conflict contracts: vocabularies, candidate shape and states.

Asserts the verbatim closed vocabularies of the conflict design (spec 4.1),
the strict UUID idempotency grammar, the candidate-versus-kind requirements
of the spec 4.1 table (a content candidate requires a verified object, a
delete candidate refuses one), the source-ID nullability rule (only a
locator collision that has not identified a canonical source), the capture
command's locator-snapshot requirement, the resolution-kind payload rules
(keep_remote/keep_local carry no object, save_merged requires one), the
aggregate read-model status shapes (open/resolving/resolved/superseded),
the frozen resolution result shapes, and that locator and key sentinels
never surface in a rendering of a frozen value.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Final
from uuid import UUID, uuid4

import pytest

from personal_os.source_conflicts.commands import (
    CaptureConflictCommand,
    ConflictResolutionResult,
    ResolveConflictCommand,
)
from personal_os.source_conflicts.contracts import (
    TERMINAL_CONFLICT_STATUSES,
    VERSION_PUBLISHING_RESOLUTIONS,
    ConflictCandidate,
    ConflictCandidateKind,
    ConflictEvidenceRole,
    ConflictIdempotencyKey,
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
    ConflictStatus,
    SourceConflict,
    validate_candidate_for_kind,
)
from personal_os.source_locators import NormalizedLocator

_CAPTURED_AT = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
_CLOSED_AT = datetime(2026, 9, 2, 12, 5, 0, tzinfo=UTC)

#: Sentinel distinguishing "derive source_id from the conflict kind" from an
#: explicit ``None`` in the builders below.
_UNSET: Final[object] = object()


def _idempotency_key() -> ConflictIdempotencyKey:
    return ConflictIdempotencyKey(str(uuid4()))


def _content_candidate() -> ConflictCandidate:
    return ConflictCandidate.content(uuid4())


# --- closed vocabularies (spec 4.1) ---------------------------------------------------------


def test_conflict_kind_values_match_the_spec_text() -> None:
    assert {kind.value for kind in ConflictKind} == {
        "stale_content",
        "edit_remote_delete",
        "delete_remote_edit",
        "locator_collision",
    }


def test_conflict_status_values_match_the_state_machine() -> None:
    assert {status.value for status in ConflictStatus} == {
        "open",
        "resolving",
        "resolved",
        "superseded",
    }


def test_terminal_conflict_statuses_are_exactly_resolved_and_superseded() -> None:
    assert (
        frozenset({ConflictStatus.RESOLVED, ConflictStatus.SUPERSEDED})
        == TERMINAL_CONFLICT_STATUSES
    )
    assert ConflictStatus.OPEN not in TERMINAL_CONFLICT_STATUSES
    assert ConflictStatus.RESOLVING not in TERMINAL_CONFLICT_STATUSES


def test_conflict_resolution_kind_values_match_the_spec_text() -> None:
    assert {kind.value for kind in ConflictResolutionKind} == {
        "keep_remote",
        "keep_local",
        "save_merged",
    }


def test_version_publishing_resolutions_are_exactly_keep_local_and_save_merged() -> None:
    assert (
        frozenset({ConflictResolutionKind.KEEP_LOCAL, ConflictResolutionKind.SAVE_MERGED})
        == VERSION_PUBLISHING_RESOLUTIONS
    )
    assert ConflictResolutionKind.KEEP_REMOTE not in VERSION_PUBLISHING_RESOLUTIONS


def test_candidate_kind_and_evidence_role_values_are_closed() -> None:
    assert {kind.value for kind in ConflictCandidateKind} == {"content", "delete"}
    assert {role.value for role in ConflictEvidenceRole} == {"base", "remote", "candidate"}


def test_conflict_resolution_outcome_values_are_closed() -> None:
    assert {outcome.value for outcome in ConflictResolutionOutcome} == {
        "resolved",
        "stale_successor",
    }


# --- strict UUID idempotency grammar --------------------------------------------------------


def test_idempotency_key_accepts_canonical_uuid_text() -> None:
    canonical = str(uuid4())
    key = ConflictIdempotencyKey(canonical)
    assert key.value == canonical
    assert key == ConflictIdempotencyKey(canonical)


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
        "",
    ],
)
def test_idempotency_key_rejects_non_canonical_text(value: str) -> None:
    with pytest.raises(ValueError, match="canonical lowercase hyphenated UUID"):
        ConflictIdempotencyKey(value)


def test_idempotency_key_rejects_nil_uuid() -> None:
    with pytest.raises(ValueError, match="non-nil"):
        ConflictIdempotencyKey("00000000-0000-0000-0000-000000000000")


def test_idempotency_key_redacts_the_raw_value_from_repr() -> None:
    canonical = str(uuid4())
    assert canonical not in repr(ConflictIdempotencyKey(canonical))


# --- candidate shape (spec 4.1 table) --------------------------------------------------------


def test_content_conflict_requires_verified_candidate_object() -> None:
    with pytest.raises(ValueError, match="verified_candidate_object_id"):
        ConflictCandidate.content(None)


def test_delete_conflict_refuses_content_object() -> None:
    with pytest.raises(ValueError, match="delete candidate"):
        ConflictCandidate.delete(verified_candidate_object_id=uuid4())


def test_content_candidate_retains_the_verified_object_reference() -> None:
    object_id = uuid4()
    candidate = ConflictCandidate.content(object_id)
    assert candidate.candidate_kind is ConflictCandidateKind.CONTENT
    assert candidate.verified_candidate_object_id == object_id


def test_content_candidate_rejects_the_nil_object_uuid() -> None:
    with pytest.raises(ValueError, match="non-nil UUID"):
        ConflictCandidate.content(UUID(int=0))


def test_delete_candidate_carries_no_content_object() -> None:
    candidate = ConflictCandidate.delete()
    assert candidate.candidate_kind is ConflictCandidateKind.DELETE
    assert candidate.verified_candidate_object_id is None


def test_candidate_kind_mapping_follows_the_spec_table() -> None:
    content = ConflictCandidate.content(uuid4())
    deletion = ConflictCandidate.delete()
    validate_candidate_for_kind(ConflictKind.STALE_CONTENT, content)
    validate_candidate_for_kind(ConflictKind.EDIT_REMOTE_DELETE, content)
    validate_candidate_for_kind(ConflictKind.DELETE_REMOTE_EDIT, deletion)
    validate_candidate_for_kind(ConflictKind.LOCATOR_COLLISION, content)
    validate_candidate_for_kind(ConflictKind.LOCATOR_COLLISION, deletion)


def test_candidate_kind_mapping_rejects_wrong_combinations() -> None:
    content = ConflictCandidate.content(uuid4())
    deletion = ConflictCandidate.delete()
    with pytest.raises(ValueError, match=r"stale_content.*content candidate"):
        validate_candidate_for_kind(ConflictKind.STALE_CONTENT, deletion)
    with pytest.raises(ValueError, match=r"edit_remote_delete.*content candidate"):
        validate_candidate_for_kind(ConflictKind.EDIT_REMOTE_DELETE, deletion)
    with pytest.raises(ValueError, match=r"delete_remote_edit.*delete candidate"):
        validate_candidate_for_kind(ConflictKind.DELETE_REMOTE_EDIT, content)


# --- capture command -------------------------------------------------------------------------


def _capture_command(
    *,
    conflict_kind: ConflictKind = ConflictKind.STALE_CONTENT,
    candidate: ConflictCandidate | None = None,
    source_id: UUID | None | object = _UNSET,
    normalized_locator: NormalizedLocator | None | object = _UNSET,
) -> CaptureConflictCommand:
    if source_id is _UNSET:
        source_id = None if conflict_kind is ConflictKind.LOCATOR_COLLISION else uuid4()
    if normalized_locator is _UNSET:
        normalized_locator = (
            NormalizedLocator("notes/planning.md")
            if conflict_kind is ConflictKind.LOCATOR_COLLISION
            else None
        )
    return CaptureConflictCommand(
        workspace_id=uuid4(),
        source_id=source_id,
        conflict_kind=conflict_kind,
        originating_event_id=uuid4(),
        originating_device_id=uuid4(),
        idempotency_key=_idempotency_key(),
        base_version_id=uuid4(),
        observed_remote_version_id=uuid4(),
        candidate=candidate if candidate is not None else _content_candidate(),
        normalized_locator=normalized_locator,
    )


def test_capture_command_binds_immutable_capture_evidence() -> None:
    command = _capture_command()
    assert command.conflict_kind is ConflictKind.STALE_CONTENT
    assert command.candidate.verified_candidate_object_id is not None
    assert command.normalized_locator is None


def _explicit_capture(
    command: CaptureConflictCommand, **overrides: object
) -> CaptureConflictCommand:
    fields: dict[str, object] = {
        "workspace_id": command.workspace_id,
        "source_id": command.source_id,
        "conflict_kind": command.conflict_kind,
        "originating_event_id": command.originating_event_id,
        "originating_device_id": command.originating_device_id,
        "idempotency_key": command.idempotency_key,
        "base_version_id": command.base_version_id,
        "observed_remote_version_id": command.observed_remote_version_id,
        "candidate": command.candidate,
        "normalized_locator": command.normalized_locator,
    }
    fields.update(overrides)
    return CaptureConflictCommand(**fields)  # type: ignore[arg-type]


def test_capture_command_rejects_nil_identity_uuids() -> None:
    command = _capture_command()
    with pytest.raises(ValueError, match="workspace_id must be a non-nil UUID"):
        _explicit_capture(command, workspace_id=UUID(int=0))
    with pytest.raises(ValueError, match="originating_event_id must be a non-nil UUID"):
        _explicit_capture(command, originating_event_id=UUID(int=0))
    with pytest.raises(ValueError, match="originating_device_id must be a non-nil UUID"):
        _explicit_capture(command, originating_device_id=UUID(int=0))


def test_capture_command_rejects_nil_optional_version_uuids() -> None:
    command = _capture_command()
    with pytest.raises(ValueError, match="base_version_id must be a non-nil UUID"):
        _explicit_capture(command, base_version_id=UUID(int=0))
    with pytest.raises(ValueError, match="observed_remote_version_id must be a non-nil UUID"):
        _explicit_capture(command, observed_remote_version_id=UUID(int=0))


def test_capture_command_rejects_delete_candidate_for_content_kinds() -> None:
    with pytest.raises(ValueError, match=r"stale_content.*content candidate"):
        _capture_command(candidate=ConflictCandidate.delete())


def test_delete_remote_edit_capture_carries_no_content_object() -> None:
    command = _capture_command(
        conflict_kind=ConflictKind.DELETE_REMOTE_EDIT,
        candidate=ConflictCandidate.delete(),
    )
    assert command.candidate.verified_candidate_object_id is None


def test_capture_command_requires_a_source_except_for_locator_collision() -> None:
    with pytest.raises(ValueError, match="source_id"):
        _capture_command(source_id=None)


def test_locator_collision_capture_requires_a_locator_snapshot() -> None:
    with pytest.raises(ValueError, match="locator_collision capture requires"):
        _capture_command(
            conflict_kind=ConflictKind.LOCATOR_COLLISION,
            source_id=uuid4(),
            normalized_locator=None,
        )


def test_locator_collision_without_source_requires_a_locator_snapshot() -> None:
    with pytest.raises(ValueError, match="without a source_id requires"):
        _capture_command(
            conflict_kind=ConflictKind.LOCATOR_COLLISION,
            source_id=None,
            normalized_locator=None,
        )


def test_capture_command_repr_never_exposes_the_locator_or_key() -> None:
    locator = NormalizedLocator("vault/private/do-not-leak.md")
    canonical = str(uuid4())
    command = CaptureConflictCommand(
        workspace_id=uuid4(),
        source_id=uuid4(),
        conflict_kind=ConflictKind.LOCATOR_COLLISION,
        originating_event_id=uuid4(),
        originating_device_id=uuid4(),
        idempotency_key=ConflictIdempotencyKey(canonical),
        base_version_id=None,
        observed_remote_version_id=None,
        candidate=_content_candidate(),
        normalized_locator=locator,
    )
    rendered = repr(command)
    assert "do-not-leak" not in rendered
    assert canonical not in rendered


# --- resolve command -------------------------------------------------------------------------


def _resolve_command(
    *,
    resolution_kind: ConflictResolutionKind = ConflictResolutionKind.KEEP_REMOTE,
    verified_candidate_object_id: UUID | None = None,
) -> ResolveConflictCommand:
    return ResolveConflictCommand(
        conflict_id=uuid4(),
        reviewed_remote_version_id=uuid4(),
        resolution_kind=resolution_kind,
        resolution_event_id=uuid4(),
        idempotency_key=_idempotency_key(),
        verified_candidate_object_id=verified_candidate_object_id,
    )


def test_resolve_command_binds_the_reviewed_remote_and_new_event_identity() -> None:
    command = _resolve_command()
    assert command.reviewed_remote_version_id is not None
    assert command.verified_candidate_object_id is None


def test_keep_remote_resolution_cannot_carry_a_verified_candidate_object() -> None:
    with pytest.raises(ValueError, match=r"keep_remote.*verified_candidate_object_id"):
        _resolve_command(
            resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
            verified_candidate_object_id=uuid4(),
        )


def test_keep_local_resolution_cannot_substitute_a_new_candidate_object() -> None:
    with pytest.raises(ValueError, match=r"keep_local.*verified_candidate_object_id"):
        _resolve_command(
            resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
            verified_candidate_object_id=uuid4(),
        )


def test_save_merged_resolution_requires_a_verified_candidate_object() -> None:
    with pytest.raises(ValueError, match=r"save_merged.*verified_candidate_object_id"):
        _resolve_command(
            resolution_kind=ConflictResolutionKind.SAVE_MERGED,
            verified_candidate_object_id=None,
        )


def test_save_merged_resolution_retains_the_verified_object_reference() -> None:
    object_id = uuid4()
    command = _resolve_command(
        resolution_kind=ConflictResolutionKind.SAVE_MERGED,
        verified_candidate_object_id=object_id,
    )
    assert command.verified_candidate_object_id == object_id


def _explicit_resolve(
    command: ResolveConflictCommand, **overrides: object
) -> ResolveConflictCommand:
    fields: dict[str, object] = {
        "conflict_id": command.conflict_id,
        "reviewed_remote_version_id": command.reviewed_remote_version_id,
        "resolution_kind": command.resolution_kind,
        "resolution_event_id": command.resolution_event_id,
        "idempotency_key": command.idempotency_key,
        "verified_candidate_object_id": command.verified_candidate_object_id,
    }
    fields.update(overrides)
    return ResolveConflictCommand(**fields)  # type: ignore[arg-type]


def test_resolve_command_rejects_nil_uuids() -> None:
    command = _resolve_command()
    with pytest.raises(ValueError, match="conflict_id must be a non-nil UUID"):
        _explicit_resolve(command, conflict_id=UUID(int=0))
    with pytest.raises(ValueError, match="resolution_event_id must be a non-nil UUID"):
        _explicit_resolve(command, resolution_event_id=UUID(int=0))
    with pytest.raises(ValueError, match="reviewed_remote_version_id must be a non-nil UUID"):
        _explicit_resolve(command, reviewed_remote_version_id=UUID(int=0))
    with pytest.raises(ValueError, match="verified_candidate_object_id must be a non-nil UUID"):
        _explicit_resolve(
            _resolve_command(
                resolution_kind=ConflictResolutionKind.SAVE_MERGED,
                verified_candidate_object_id=uuid4(),
            ),
            verified_candidate_object_id=UUID(int=0),
        )


# --- aggregate read model --------------------------------------------------------------------


def _open_conflict(
    *,
    conflict_kind: ConflictKind = ConflictKind.STALE_CONTENT,
    candidate: ConflictCandidate | None = None,
    source_id: UUID | None | object = _UNSET,
    status: ConflictStatus = ConflictStatus.OPEN,
    resolution_kind: ConflictResolutionKind | None = None,
    resolution_event_id: UUID | None = None,
    resulting_version_id: UUID | None = None,
    successor_conflict_id: UUID | None = None,
    closed_at: datetime | None = None,
    captured_at: datetime = _CAPTURED_AT,
    observed_remote_version_id: UUID | None = None,
) -> SourceConflict:
    if source_id is _UNSET:
        source_id = None if conflict_kind is ConflictKind.LOCATOR_COLLISION else uuid4()
    if observed_remote_version_id is None:
        observed_remote_version_id = uuid4()
    return SourceConflict(
        conflict_id=uuid4(),
        workspace_id=uuid4(),
        source_id=source_id,
        conflict_kind=conflict_kind,
        status=status,
        originating_event_id=uuid4(),
        originating_device_id=uuid4(),
        base_version_id=uuid4(),
        observed_remote_version_id=observed_remote_version_id,
        candidate=candidate if candidate is not None else _content_candidate(),
        captured_at=captured_at,
        resolution_kind=resolution_kind,
        resolution_event_id=resolution_event_id,
        resulting_version_id=resulting_version_id,
        successor_conflict_id=successor_conflict_id,
        closed_at=closed_at,
    )


def test_open_conflict_carries_no_resolution_evidence() -> None:
    conflict = _open_conflict()
    assert conflict.status is ConflictStatus.OPEN
    assert conflict.resolution_kind is None
    assert conflict.resolution_event_id is None
    assert conflict.resulting_version_id is None
    assert conflict.successor_conflict_id is None
    assert conflict.closed_at is None


def test_open_conflict_rejects_resolution_evidence() -> None:
    with pytest.raises(ValueError, match="open conflict carries no resolution"):
        _open_conflict(resolution_event_id=uuid4())


def test_resolving_conflict_requires_resolution_evidence_but_no_outcome() -> None:
    conflict = _open_conflict(
        status=ConflictStatus.RESOLVING,
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
        resolution_event_id=uuid4(),
    )
    assert conflict.status is ConflictStatus.RESOLVING
    assert conflict.resulting_version_id is None
    assert conflict.closed_at is None


def test_resolving_conflict_rejects_a_resulting_version() -> None:
    with pytest.raises(ValueError, match="resolving conflict carries no resulting"):
        _open_conflict(
            status=ConflictStatus.RESOLVING,
            resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
            resolution_event_id=uuid4(),
            resulting_version_id=uuid4(),
        )


def test_resolved_keep_remote_conflict_creates_no_source_version() -> None:
    conflict = _open_conflict(
        status=ConflictStatus.RESOLVED,
        resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
        resolution_event_id=uuid4(),
        closed_at=_CLOSED_AT,
    )
    assert conflict.resulting_version_id is None


def test_resolved_conflict_rejects_a_version_under_keep_remote() -> None:
    with pytest.raises(ValueError, match="keep_remote resolution creates no source version"):
        _open_conflict(
            status=ConflictStatus.RESOLVED,
            resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
            resolution_event_id=uuid4(),
            resulting_version_id=uuid4(),
            closed_at=_CLOSED_AT,
        )


def test_resolved_keep_local_conflict_binds_exactly_one_resulting_version() -> None:
    version_id = uuid4()
    conflict = _open_conflict(
        status=ConflictStatus.RESOLVED,
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
        resolution_event_id=uuid4(),
        resulting_version_id=version_id,
        closed_at=_CLOSED_AT,
    )
    assert conflict.resulting_version_id == version_id


@pytest.mark.parametrize(
    "resolution_kind",
    sorted(VERSION_PUBLISHING_RESOLUTIONS, key=lambda kind: kind.value),
)
def test_resolved_conflict_requires_a_version_for_publishing_kinds(
    resolution_kind: ConflictResolutionKind,
) -> None:
    with pytest.raises(ValueError, match="resolution requires a resulting_version_id"):
        _open_conflict(
            status=ConflictStatus.RESOLVED,
            resolution_kind=resolution_kind,
            resolution_event_id=uuid4(),
            closed_at=_CLOSED_AT,
        )


def test_resolved_conflict_requires_the_terminal_evidence() -> None:
    with pytest.raises(ValueError, match="resolved conflict requires"):
        _open_conflict(
            status=ConflictStatus.RESOLVED,
            resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
            resolution_event_id=uuid4(),
        )


def test_resolved_conflict_rejects_a_successor() -> None:
    with pytest.raises(ValueError, match="resolved conflict carries no successor"):
        _open_conflict(
            status=ConflictStatus.RESOLVED,
            resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
            resolution_event_id=uuid4(),
            closed_at=_CLOSED_AT,
            successor_conflict_id=uuid4(),
        )


def test_superseded_conflict_binds_the_stale_attempt_and_successor() -> None:
    successor_id = uuid4()
    conflict = _open_conflict(
        status=ConflictStatus.SUPERSEDED,
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
        resolution_event_id=uuid4(),
        successor_conflict_id=successor_id,
        closed_at=_CLOSED_AT,
    )
    assert conflict.successor_conflict_id == successor_id
    assert conflict.resulting_version_id is None


def test_superseded_conflict_requires_the_terminal_evidence() -> None:
    with pytest.raises(ValueError, match="superseded conflict requires"):
        _open_conflict(status=ConflictStatus.SUPERSEDED, resolution_event_id=uuid4())


def test_superseded_conflict_carries_no_resulting_version() -> None:
    with pytest.raises(ValueError, match="superseded conflict carries no resulting"):
        _open_conflict(
            status=ConflictStatus.SUPERSEDED,
            resolution_event_id=uuid4(),
            successor_conflict_id=uuid4(),
            resulting_version_id=uuid4(),
            closed_at=_CLOSED_AT,
        )


def _explicit_conflict(conflict: SourceConflict, **overrides: object) -> SourceConflict:
    fields: dict[str, object] = {
        "conflict_id": conflict.conflict_id,
        "workspace_id": conflict.workspace_id,
        "source_id": conflict.source_id,
        "conflict_kind": conflict.conflict_kind,
        "status": conflict.status,
        "originating_event_id": conflict.originating_event_id,
        "originating_device_id": conflict.originating_device_id,
        "base_version_id": conflict.base_version_id,
        "observed_remote_version_id": conflict.observed_remote_version_id,
        "candidate": conflict.candidate,
        "captured_at": conflict.captured_at,
        "resolution_kind": conflict.resolution_kind,
        "resolution_event_id": conflict.resolution_event_id,
        "resulting_version_id": conflict.resulting_version_id,
        "successor_conflict_id": conflict.successor_conflict_id,
        "closed_at": conflict.closed_at,
    }
    fields.update(overrides)
    return SourceConflict(**fields)  # type: ignore[arg-type]


def test_superseded_conflict_successor_must_differ_from_the_conflict() -> None:
    conflict = _open_conflict(
        status=ConflictStatus.SUPERSEDED,
        resolution_event_id=uuid4(),
        successor_conflict_id=uuid4(),
        closed_at=_CLOSED_AT,
    )
    with pytest.raises(ValueError, match="must differ"):
        _explicit_conflict(conflict, successor_conflict_id=conflict.conflict_id)


def test_delete_remote_edit_conflict_refuses_a_content_candidate() -> None:
    with pytest.raises(ValueError, match=r"delete_remote_edit.*delete candidate"):
        _open_conflict(
            conflict_kind=ConflictKind.DELETE_REMOTE_EDIT,
            candidate=ConflictCandidate.content(uuid4()),
        )


def test_locator_collision_conflict_accepts_either_candidate_shape() -> None:
    deletion = _open_conflict(
        conflict_kind=ConflictKind.LOCATOR_COLLISION,
        candidate=ConflictCandidate.delete(),
    )
    content = _open_conflict(
        conflict_kind=ConflictKind.LOCATOR_COLLISION,
        candidate=ConflictCandidate.content(uuid4()),
    )
    assert deletion.source_id is None
    assert content.source_id is None


def test_non_locator_conflicts_require_a_source() -> None:
    with pytest.raises(ValueError, match="source_id"):
        _open_conflict(source_id=None)


def test_conflict_rejects_nil_identity_uuids() -> None:
    conflict = _open_conflict()
    with pytest.raises(ValueError, match="conflict_id must be a non-nil UUID"):
        _explicit_conflict(conflict, conflict_id=UUID(int=0))
    with pytest.raises(ValueError, match="workspace_id must be a non-nil UUID"):
        _explicit_conflict(conflict, workspace_id=UUID(int=0))
    with pytest.raises(ValueError, match="originating_event_id must be a non-nil UUID"):
        _explicit_conflict(conflict, originating_event_id=UUID(int=0))
    with pytest.raises(ValueError, match="originating_device_id must be a non-nil UUID"):
        _explicit_conflict(conflict, originating_device_id=UUID(int=0))


def test_conflict_requires_aware_utc_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _open_conflict(captured_at=datetime(2026, 9, 2, 12, 0, 0))


def test_conflict_normalizes_aware_timestamps_to_utc() -> None:
    offset_zone = timezone(timedelta(hours=7))
    captured = datetime(2026, 9, 2, 19, 0, 0, tzinfo=offset_zone)
    conflict = _open_conflict(captured_at=captured)
    assert conflict.captured_at == captured.astimezone(UTC)
    closed = datetime(2026, 9, 2, 19, 5, 0, tzinfo=offset_zone)
    resolved = _open_conflict(
        status=ConflictStatus.RESOLVED,
        resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
        resolution_event_id=uuid4(),
        closed_at=closed,
    )
    assert resolved.closed_at == closed.astimezone(UTC)


# --- frozen resolution result ----------------------------------------------------------------


def _resolution_result(
    *,
    kind: ConflictResolutionOutcome = ConflictResolutionOutcome.RESOLVED,
    resolution_kind: ConflictResolutionKind = ConflictResolutionKind.KEEP_REMOTE,
    resulting_version_id: UUID | None = None,
    successor: SourceConflict | None = None,
) -> ConflictResolutionResult:
    return ConflictResolutionResult(
        kind=kind,
        conflict_id=uuid4(),
        resolution_event_id=uuid4(),
        resolution_kind=resolution_kind,
        resulting_version_id=resulting_version_id,
        successor=successor,
        completed_at=_CLOSED_AT,
    )


def test_resolved_result_replays_identically() -> None:
    result = _resolution_result()
    same = ConflictResolutionResult(
        kind=result.kind,
        conflict_id=result.conflict_id,
        resolution_event_id=result.resolution_event_id,
        resolution_kind=result.resolution_kind,
        resulting_version_id=result.resulting_version_id,
        successor=result.successor,
        completed_at=result.completed_at,
    )
    assert result == same


def test_resolved_result_carries_no_successor() -> None:
    with pytest.raises(ValueError, match="resolved result carries no successor"):
        _resolution_result(successor=_open_conflict())


def test_publishing_resolved_result_requires_a_version() -> None:
    with pytest.raises(ValueError, match="resolution requires a resulting_version_id"):
        _resolution_result(resolution_kind=ConflictResolutionKind.SAVE_MERGED)


def test_stale_successor_result_requires_the_successor_conflict() -> None:
    with pytest.raises(ValueError, match="stale_successor result requires a successor"):
        _resolution_result(kind=ConflictResolutionOutcome.STALE_SUCCESSOR)


def test_stale_successor_result_binds_the_newer_observed_remote() -> None:
    newer_version_id = uuid4()
    successor = _open_conflict(observed_remote_version_id=newer_version_id)
    result = _resolution_result(
        kind=ConflictResolutionOutcome.STALE_SUCCESSOR,
        successor=successor,
    )
    assert result.successor is not None
    assert result.successor.observed_remote_version_id == newer_version_id
    assert result.resulting_version_id is None


def test_stale_successor_result_carries_no_resulting_version() -> None:
    with pytest.raises(ValueError, match="stale_successor result carries no resulting"):
        _resolution_result(
            kind=ConflictResolutionOutcome.STALE_SUCCESSOR,
            successor=_open_conflict(),
            resulting_version_id=uuid4(),
        )


def test_resolution_result_requires_aware_utc_completion() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ConflictResolutionResult(
            kind=ConflictResolutionOutcome.RESOLVED,
            conflict_id=uuid4(),
            resolution_event_id=uuid4(),
            resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
            resulting_version_id=None,
            successor=None,
            completed_at=datetime(2026, 9, 2, 12, 5, 0),
        )
