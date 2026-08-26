"""Closed device-sync contracts: vocabularies, bounds, shapes and limits.

Asserts the exact enum vocabularies, the pull/page/run limit trio of the
global constraints, the one-hour manifest-run lifetime input, the strict UUID
and non-negative bounds, the operation-shaped locator/tombstone matrix of
spec 7.1 (create/update never carry tombstone operands; delete/restore carry
their exact tombstone/locator shapes), the unknown-enum-string boundary that
fails before any store call, and the redacted ``repr`` contract that never
renders a raw locator or full digest.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid7

import pytest
from tests.unit.device_sync.fakes import (
    COMMITTED_AT,
    FINGERPRINT,
    LOCATOR,
    build_action_page,
    build_actions_query,
    build_append_command,
    build_complete_command,
    build_device_sync_context,
    build_device_sync_event,
    build_diagnostic_context,
    build_page_receipt,
    build_run_receipt,
)

from personal_os.device_sync.contracts import (
    MANIFEST_RUN_LIFETIME,
    MAX_MANIFEST_PAGE_ENTRIES,
    MAX_MANIFEST_RUN_ENTRIES,
    MAX_PULL_EVENTS,
    AppendManifestPageCommand,
    DeviceContentDescriptor,
    DeviceCursorReceipt,
    DeviceEventPage,
    DeviceEventType,
    DeviceSyncContext,
    DeviceSyncEvent,
    FinalizeManifestCommand,
    ManifestAction,
    ManifestActionKind,
    ManifestActionPage,
    ManifestActionsQuery,
    ManifestEntry,
    ManifestRunState,
    SourceFingerprint,
    StartManifestCommand,
    compute_manifest_run_expiry,
)
from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject

#: One valid 64-character lowercase hexadecimal digest reused by builders.
DIGEST_TEXT = "a" * 64
OTHER_DIGEST_TEXT = "b" * 64

#: The error-message phrase each event type contributes to its shape errors.
_SHAPE_PHRASE = {
    DeviceEventType.CREATED: "create",
    DeviceEventType.UPDATED: "update",
    DeviceEventType.RENAMED: "rename",
    DeviceEventType.MOVED: "move",
    DeviceEventType.DELETED: "delete",
    DeviceEventType.RESTORED: "restore",
}


def _fingerprint(sha256: str = DIGEST_TEXT) -> SourceFingerprint:
    return SourceFingerprint(sha256=sha256, size_bytes=16, media_type="text/markdown")


def _shape_error(event_type: DeviceEventType) -> str:
    return f"{_SHAPE_PHRASE[event_type]} event shape invalid"


def test_device_event_type_vocabulary_is_closed() -> None:
    assert {event_type.value for event_type in DeviceEventType} == {
        "created",
        "updated",
        "renamed",
        "moved",
        "deleted",
        "restored",
    }


def test_manifest_run_state_vocabulary_is_closed() -> None:
    assert {state.value for state in ManifestRunState} == {
        "collecting",
        "planned",
        "applying",
        "completed",
        "expired",
        "failed",
    }


def test_manifest_action_kind_vocabulary_is_closed() -> None:
    assert {kind.value for kind in ManifestActionKind} == {
        "upload",
        "download",
        "apply_tombstone",
        "conflict",
        "no_change",
        "excluded",
    }


def test_unknown_enum_strings_fail_before_any_store_call() -> None:
    with pytest.raises(ValueError):
        DeviceEventType("recreated")
    with pytest.raises(ValueError):
        ManifestRunState("queued")
    with pytest.raises(ValueError):
        ManifestActionKind("replace")


def test_pull_page_and_run_limits_are_pinned() -> None:
    assert MAX_PULL_EVENTS == 200
    assert MAX_MANIFEST_PAGE_ENTRIES == 500
    assert MAX_MANIFEST_RUN_ENTRIES == 100_000


def test_manifest_run_lifetime_is_exactly_one_hour() -> None:
    assert timedelta(hours=1) == MANIFEST_RUN_LIFETIME
    created_at = datetime(2026, 8, 26, 9, 0, 0, tzinfo=timezone(timedelta(hours=7)))
    expires_at = compute_manifest_run_expiry(created_at)
    assert expires_at == datetime(2026, 8, 26, 3, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_manifest_run_expiry(datetime(2026, 8, 26, 9, 0, 0))


@pytest.mark.parametrize("field_name", ["workspace_id", "device_id", "user_id"])
def test_device_sync_context_rejects_nil_uuids(field_name: str) -> None:
    fields = {"workspace_id": uuid4(), "device_id": uuid4(), "user_id": uuid4()}
    fields[field_name] = UUID(int=0)
    with pytest.raises(ValueError, match=f"{field_name} must be a non-nil UUID"):
        DeviceSyncContext(**fields)


def test_source_fingerprint_rejects_non_canonical_evidence() -> None:
    with pytest.raises(ValueError, match="sha256"):
        _fingerprint(sha256="A" * 64)
    with pytest.raises(ValueError, match="sha256"):
        _fingerprint(sha256="a" * 63)
    with pytest.raises(ValueError, match="size_bytes"):
        SourceFingerprint(sha256=DIGEST_TEXT, size_bytes=-1, media_type="text/markdown")
    with pytest.raises(ValueError, match="media type"):
        SourceFingerprint(sha256=DIGEST_TEXT, size_bytes=1, media_type="Text/Markdown")
    with pytest.raises(ValueError, match="media type"):
        SourceFingerprint(
            sha256=DIGEST_TEXT, size_bytes=1, media_type="text/markdown; charset=utf-8"
        )


def test_source_fingerprint_repr_never_renders_the_digest() -> None:
    assert DIGEST_TEXT not in repr(_fingerprint())
    assert "<redacted>" in repr(_fingerprint())


@pytest.mark.parametrize("event_type", tuple(DeviceEventType))
def test_every_event_type_has_a_valid_operation_shape(event_type: DeviceEventType) -> None:
    event = build_device_sync_event(event_type)
    assert event.event_type is event_type
    assert event.event_sequence >= 0


@pytest.mark.parametrize(
    "event_type",
    [DeviceEventType.CREATED, DeviceEventType.UPDATED],
)
def test_create_and_update_events_cannot_carry_tombstone_operands(
    event_type: DeviceEventType,
) -> None:
    event = build_device_sync_event(event_type)
    with pytest.raises(ValueError, match=_shape_error(event_type)):
        replace(event, tombstone_id=uuid4())


def test_delete_event_requires_prior_locator_and_tombstone() -> None:
    with pytest.raises(ValueError, match="delete event shape invalid"):
        DeviceSyncEvent(
            event_id=uuid7(),
            event_sequence=4,
            event_type=DeviceEventType.DELETED,
            source_id=uuid4(),
            origin_device_id=uuid4(),
            base_version_id=uuid4(),
            current_version_id=uuid4(),
            base_fingerprint=FINGERPRINT,
            current_fingerprint=None,
            prior_locator=None,
            resulting_locator=None,
            tombstone_id=None,
            committed_at=COMMITTED_AT,
        )


@pytest.mark.parametrize(
    "event_type",
    [
        DeviceEventType.CREATED,
        DeviceEventType.RENAMED,
        DeviceEventType.MOVED,
        DeviceEventType.RESTORED,
    ],
)
def test_events_with_resulting_locators_require_them(event_type: DeviceEventType) -> None:
    event = build_device_sync_event(event_type)
    with pytest.raises(ValueError, match=_shape_error(event_type)):
        replace(event, resulting_locator=None)


@pytest.mark.parametrize("event_type", [DeviceEventType.UPDATED, DeviceEventType.DELETED])
def test_events_without_resulting_locators_forbid_them(event_type: DeviceEventType) -> None:
    event = build_device_sync_event(event_type)
    with pytest.raises(ValueError, match=_shape_error(event_type)):
        replace(event, resulting_locator=LOCATOR)


@pytest.mark.parametrize(
    "event_type",
    [DeviceEventType.RENAMED, DeviceEventType.MOVED, DeviceEventType.DELETED],
)
def test_rename_move_and_delete_require_the_prior_locator(event_type: DeviceEventType) -> None:
    event = build_device_sync_event(event_type)
    with pytest.raises(ValueError, match=_shape_error(event_type)):
        replace(event, prior_locator=None)


@pytest.mark.parametrize(
    "event_type",
    [DeviceEventType.CREATED, DeviceEventType.UPDATED, DeviceEventType.RESTORED],
)
def test_events_without_prior_locators_forbid_them(event_type: DeviceEventType) -> None:
    event = build_device_sync_event(event_type)
    with pytest.raises(ValueError, match=_shape_error(event_type)):
        replace(event, prior_locator=LOCATOR)


def test_restore_event_requires_its_tombstone() -> None:
    event = build_device_sync_event(DeviceEventType.RESTORED)
    with pytest.raises(ValueError, match="restore event shape invalid"):
        replace(event, tombstone_id=None)


def test_delete_event_requires_its_tombstone() -> None:
    event = build_device_sync_event(DeviceEventType.DELETED)
    with pytest.raises(ValueError, match="delete event shape invalid"):
        replace(event, tombstone_id=None)


def test_event_rejects_negative_sequence_nil_source_and_naive_commit_time() -> None:
    event = build_device_sync_event(DeviceEventType.UPDATED)
    with pytest.raises(ValueError, match="event_sequence"):
        replace(event, event_sequence=-1)
    with pytest.raises(ValueError, match="source_id"):
        replace(event, source_id=UUID(int=0))
    with pytest.raises(ValueError, match="committed_at"):
        replace(event, committed_at=datetime(2026, 8, 26, 9, 0, 0))


def test_event_repr_never_renders_locator_or_digest() -> None:
    event = build_device_sync_event(DeviceEventType.CREATED)
    rendered = repr(event)
    assert LOCATOR.value not in rendered
    assert DIGEST_TEXT not in rendered
    assert "<redacted>" in rendered


def test_cursor_receipt_rejects_negative_and_regressed_watermarks() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        DeviceCursorReceipt(acknowledged_sequence=-1, delivered_through_sequence=0)
    with pytest.raises(ValueError, match="delivered"):
        DeviceCursorReceipt(acknowledged_sequence=5, delivered_through_sequence=4)


def test_event_page_enforces_watermark_order_and_the_pull_limit() -> None:
    events = tuple(build_device_sync_event(DeviceEventType.UPDATED) for _ in range(3))
    page = DeviceEventPage(
        acknowledged_sequence=0,
        page_checkpoint_sequence=3,
        delivered_through_sequence=3,
        events=events,
        has_more=True,
    )
    assert page.events == events
    with pytest.raises(ValueError, match="checkpoint"):
        DeviceEventPage(
            acknowledged_sequence=0,
            page_checkpoint_sequence=2,
            delivered_through_sequence=3,
            events=events,
            has_more=False,
        )
    with pytest.raises(ValueError, match="delivered"):
        DeviceEventPage(
            acknowledged_sequence=3,
            page_checkpoint_sequence=3,
            delivered_through_sequence=2,
            events=(),
            has_more=False,
        )
    with pytest.raises(ValueError, match=f"at most {MAX_PULL_EVENTS} events"):
        DeviceEventPage(
            acknowledged_sequence=0,
            page_checkpoint_sequence=MAX_PULL_EVENTS + 1,
            delivered_through_sequence=MAX_PULL_EVENTS + 1,
            events=tuple(
                build_device_sync_event(DeviceEventType.UPDATED) for _ in range(MAX_PULL_EVENTS + 1)
            ),
            has_more=False,
        )


def test_content_descriptor_projects_the_expected_object() -> None:
    digest = ContentDigest.parse(DIGEST_TEXT)
    media_type = CanonicalMediaType.parse("text/markdown")
    descriptor = DeviceContentDescriptor(
        source_id=uuid4(),
        source_version_id=uuid4(),
        content_digest=digest,
        size_bytes=64,
        media_type=media_type,
    )
    expected = descriptor.expected_object()
    assert isinstance(expected, ExpectedObject)
    assert expected.content_digest == digest
    assert expected.size_bytes == 64
    assert expected.media_type == media_type
    assert DIGEST_TEXT not in repr(descriptor)


def test_content_descriptor_rejects_nil_uuids_and_negative_sizes() -> None:
    digest = ContentDigest.parse(DIGEST_TEXT)
    media_type = CanonicalMediaType.parse("text/markdown")
    with pytest.raises(ValueError, match="source_id"):
        DeviceContentDescriptor(
            source_id=UUID(int=0),
            source_version_id=uuid4(),
            content_digest=digest,
            size_bytes=1,
            media_type=media_type,
        )
    with pytest.raises(ValueError, match="source_version_id"):
        DeviceContentDescriptor(
            source_id=uuid4(),
            source_version_id=UUID(int=0),
            content_digest=digest,
            size_bytes=1,
            media_type=media_type,
        )
    with pytest.raises(ValueError, match="size_bytes"):
        DeviceContentDescriptor(
            source_id=uuid4(),
            source_version_id=uuid4(),
            content_digest=digest,
            size_bytes=-1,
            media_type=media_type,
        )


def _build_entry(local_entry_id: str, observation_generation: int = 0) -> ManifestEntry:
    return ManifestEntry(
        local_entry_id=local_entry_id,
        known_source_id=None,
        known_version_id=None,
        normalized_locator=LOCATOR,
        fingerprint=_fingerprint(),
        observation_generation=observation_generation,
    )


def test_manifest_entry_bounds_its_local_evidence() -> None:
    with pytest.raises(ValueError, match="local_entry_id"):
        _build_entry("")
    with pytest.raises(ValueError, match="local_entry_id"):
        _build_entry("x" * 257)
    with pytest.raises(ValueError, match="known_source_id"):
        replace(_build_entry("entry-1"), known_source_id=UUID(int=0))
    with pytest.raises(ValueError, match="observation_generation"):
        _build_entry("entry-1", observation_generation=-1)


def test_manifest_entry_repr_never_renders_locator_or_digest() -> None:
    rendered = repr(_build_entry("entry-1"))
    assert LOCATOR.value not in rendered
    assert DIGEST_TEXT not in rendered


def test_manifest_action_bounds_its_indices_and_uuids() -> None:
    action = ManifestAction(
        action_index=0,
        action_kind=ManifestActionKind.UPLOAD,
        local_entry_id="entry-1",
        source_id=None,
        source_version_id=None,
        source_locator_id=None,
        source_tombstone_id=None,
        reason=None,
    )
    with pytest.raises(ValueError, match="action_index"):
        replace(action, action_index=-1)
    with pytest.raises(ValueError, match="source_id"):
        replace(action, source_id=UUID(int=0))


def test_download_action_requires_its_checkpoint_locator() -> None:
    """The download placement operand is the checkpoint-active locator text
    the device must place bytes at (spec 12.3, task 11b): a download action
    without it can never converge, so the shape fails closed."""

    with pytest.raises(ValueError, match="download action shape invalid"):
        ManifestAction(
            action_index=0,
            action_kind=ManifestActionKind.DOWNLOAD,
            local_entry_id=None,
            source_id=uuid4(),
            source_version_id=uuid4(),
            source_locator_id=uuid4(),
            source_tombstone_id=None,
            reason=None,
        )


def test_non_download_actions_forbid_the_checkpoint_locator() -> None:
    action = ManifestAction(
        action_index=0,
        action_kind=ManifestActionKind.UPLOAD,
        local_entry_id="entry-1",
        source_id=None,
        source_version_id=None,
        source_locator_id=None,
        source_tombstone_id=None,
        reason=None,
    )
    with pytest.raises(ValueError, match="upload action shape invalid"):
        replace(action, checkpoint_locator=LOCATOR)
    with pytest.raises(ValueError, match="conflict action shape invalid"):
        replace(
            action,
            action_kind=ManifestActionKind.CONFLICT,
            reason=None,
            checkpoint_locator=LOCATOR,
        )


def test_manifest_action_repr_never_renders_the_locator() -> None:
    action = ManifestAction(
        action_index=0,
        action_kind=ManifestActionKind.DOWNLOAD,
        local_entry_id=None,
        source_id=uuid4(),
        source_version_id=uuid4(),
        source_locator_id=uuid4(),
        source_tombstone_id=None,
        reason=None,
        checkpoint_locator=LOCATOR,
    )
    rendered = repr(action)
    assert LOCATOR.value not in rendered
    assert "<redacted>" in rendered


def test_start_command_rejects_negative_observation_generation() -> None:
    with pytest.raises(ValueError, match="client_observation_generation"):
        StartManifestCommand(
            context=build_device_sync_context(),
            client_observation_generation=-1,
            diagnostic_context=build_diagnostic_context(),
        )


def test_append_page_command_enforces_the_page_entry_limit() -> None:
    command = build_append_command(build_device_sync_context())
    assert len(command.entries) == 1
    oversized = tuple(
        _build_entry(f"entry-{index}") for index in range(MAX_MANIFEST_PAGE_ENTRIES + 1)
    )
    with pytest.raises(ValueError, match=f"at most {MAX_MANIFEST_PAGE_ENTRIES} entries"):
        AppendManifestPageCommand(
            context=command.context,
            manifest_run_id=command.manifest_run_id,
            page_number=0,
            entries=oversized,
            page_digest=command.page_digest,
            diagnostic_context=command.diagnostic_context,
        )
    with pytest.raises(ValueError, match="page_number"):
        AppendManifestPageCommand(
            context=command.context,
            manifest_run_id=command.manifest_run_id,
            page_number=-1,
            entries=command.entries,
            page_digest=command.page_digest,
            diagnostic_context=command.diagnostic_context,
        )


def test_finalize_command_enforces_the_run_entry_limit() -> None:
    context = build_device_sync_context()
    diagnostic_context = build_diagnostic_context()
    FinalizeManifestCommand(
        context=context,
        manifest_run_id=uuid4(),
        total_entry_count=MAX_MANIFEST_RUN_ENTRIES,
        final_digest=ContentDigest.parse(OTHER_DIGEST_TEXT),
        diagnostic_context=diagnostic_context,
    )
    with pytest.raises(ValueError, match=f"at most {MAX_MANIFEST_RUN_ENTRIES} entries"):
        FinalizeManifestCommand(
            context=context,
            manifest_run_id=uuid4(),
            total_entry_count=MAX_MANIFEST_RUN_ENTRIES + 1,
            final_digest=ContentDigest.parse(OTHER_DIGEST_TEXT),
            diagnostic_context=diagnostic_context,
        )


def test_actions_query_bounds_its_page_window() -> None:
    query = build_actions_query(build_device_sync_context())
    with pytest.raises(ValueError, match="limit"):
        ManifestActionsQuery(
            context=query.context,
            manifest_run_id=query.manifest_run_id,
            after_action_index=0,
            limit=0,
            diagnostic_context=query.diagnostic_context,
        )
    with pytest.raises(ValueError, match="limit"):
        ManifestActionsQuery(
            context=query.context,
            manifest_run_id=query.manifest_run_id,
            after_action_index=0,
            limit=MAX_MANIFEST_PAGE_ENTRIES + 1,
            diagnostic_context=query.diagnostic_context,
        )
    with pytest.raises(ValueError, match="after_action_index"):
        ManifestActionsQuery(
            context=query.context,
            manifest_run_id=query.manifest_run_id,
            after_action_index=-1,
            limit=200,
            diagnostic_context=query.diagnostic_context,
        )


def test_run_receipt_enforces_bounds_and_expiry_shape() -> None:
    receipt = build_run_receipt()
    with pytest.raises(ValueError, match="entry_count"):
        replace(receipt, entry_count=MAX_MANIFEST_RUN_ENTRIES + 1)
    with pytest.raises(ValueError, match="checkpoint"):
        replace(receipt, base_acknowledged_sequence=6, checkpoint_sequence=5)
    with pytest.raises(ValueError, match="expires_at"):
        replace(receipt, expires_at=datetime(2026, 8, 26, 10, 0, 0))


def test_page_receipt_bounds_its_counts() -> None:
    receipt = build_page_receipt()
    with pytest.raises(ValueError, match="accepted_entry_count"):
        replace(receipt, accepted_entry_count=-1)


def test_action_page_requires_strictly_increasing_indices() -> None:
    page = build_action_page()
    duplicated = replace(page.actions[1], action_index=0)
    with pytest.raises(ValueError, match="strictly increasing action indices"):
        ManifestActionPage(
            manifest_run_id=page.manifest_run_id,
            actions=(page.actions[0], duplicated),
            has_more=False,
        )


def test_complete_command_carries_the_exact_digest() -> None:
    command = build_complete_command(build_device_sync_context())
    assert command.final_digest == ContentDigest.parse(command.final_digest.hexadecimal)
