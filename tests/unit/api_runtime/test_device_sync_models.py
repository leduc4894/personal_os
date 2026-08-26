"""Device sync wire models: strict bodies, boundary conversion and rendering.

These tests pin the strict boundary between the device sync wire grammar and
the frozen domain values of spec 7: every request model is closed for extra
properties and admits no workspace, device or user selector; entry and digest
conversion surfaces the closed ``device_manifest_page_invalid`` /
``device_manifest_digest_mismatch`` codes instead of echoing a rejected
value; and the response renderers project the frozen domain results onto
exactly their safe members — never a receipt, object key, provider detail or
full locator text beyond the canonical operand the spec publishes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from api_runtime.device_sync_models import (
    CursorAcknowledgementRequest,
    DeviceCursorReceiptData,
    DeviceEventPageData,
    DeviceSyncEventData,
    ManifestActionData,
    ManifestActionPageData,
    ManifestCompleteRequest,
    ManifestEntryRequest,
    ManifestFinalizeRequest,
    ManifestPageReceiptData,
    ManifestPageRequest,
    ManifestRunReceiptData,
    ManifestStartRequest,
    SourceFingerprintData,
    device_cursor_receipt_data,
    device_event_page_data,
    manifest_action_page_data,
    manifest_page_receipt_data,
    manifest_run_receipt_data,
    parse_final_digest,
    parse_page_digest,
    to_domain_entries,
)
from pydantic import ValidationError

from personal_os.device_sync.contracts import (
    MAX_MANIFEST_PAGE_ENTRIES,
    DeviceCursorReceipt,
    DeviceEventPage,
    DeviceEventType,
    DeviceSyncEvent,
    ManifestAction,
    ManifestActionKind,
    ManifestActionPage,
    ManifestActionReason,
    ManifestPageReceipt,
    ManifestRunReceipt,
    ManifestRunState,
    SourceFingerprint,
)
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.source_locators.values import NormalizedLocator

_COMMITTED_AT = datetime(2026, 8, 26, 9, 30, 0, tzinfo=UTC)
_EXPIRES_AT = datetime(2026, 8, 26, 11, 0, 0, tzinfo=UTC)
_SHA256 = "a" * 64


def _fingerprint_wire() -> dict[str, Any]:
    return {"sha256": _SHA256, "size_bytes": 128, "media_type": "text/markdown"}


def _entry_wire(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "local_entry_id": "note-1",
        "known_source_id": None,
        "known_version_id": None,
        "normalized_locator": "notes/note.md",
        "fingerprint": _fingerprint_wire(),
        "observation_generation": 4,
    }
    body.update(overrides)
    return body


# --- strict request bodies --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "valid_body"),
    [
        (
            CursorAcknowledgementRequest,
            {"expected_previous_sequence": 0, "applied_through_sequence": 0},
        ),
        (ManifestStartRequest, {"client_observation_generation": 0}),
        (ManifestEntryRequest, None),  # built through _entry_wire below
        (ManifestPageRequest, {"entries": [], "page_digest": _SHA256}),
        (ManifestFinalizeRequest, {"total_entry_count": 0, "final_digest": _SHA256}),
        (ManifestCompleteRequest, {"final_digest": _SHA256}),
        (
            SourceFingerprintData,
            {"sha256": _SHA256, "size_bytes": 1, "media_type": "text/markdown"},
        ),
    ],
)
def test_every_request_model_is_frozen_and_closed(
    model: type[Any], valid_body: dict[str, Any] | None
) -> None:
    body = _entry_wire() if valid_body is None else valid_body
    instance = model.model_validate(body)
    with pytest.raises(ValidationError):
        instance.unexpected_member = "x"  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        model.model_validate({**body, "unexpected_member": "x"})


def test_cursor_acknowledgement_body_admits_no_identity_selector() -> None:
    body = CursorAcknowledgementRequest(
        expected_previous_sequence=3, applied_through_sequence=7
    )
    assert body.expected_previous_sequence == 3
    assert body.applied_through_sequence == 7
    with pytest.raises(ValidationError):
        CursorAcknowledgementRequest.model_validate(
            {
                "expected_previous_sequence": 3,
                "applied_through_sequence": 7,
                "workspace_id": str(uuid4()),
            }
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"workspace_id": str(uuid4())},
        {"device_id": str(uuid4())},
        {"user_id": str(uuid4())},
        {"observation_generation": -1},
        {"local_entry_id": ""},
        {"local_entry_id": "x" * 257},
        {"fingerprint": {"sha256": "nope", "size_bytes": 1, "media_type": "text/markdown"}},
        {"fingerprint": {"sha256": _SHA256, "size_bytes": -1, "media_type": "text/markdown"}},
    ],
)
def test_manifest_entry_rejects_non_contract_bodies(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ManifestEntryRequest.model_validate(_entry_wire(**overrides))


def test_manifest_page_bounds_the_entry_count_and_digest_grammar() -> None:
    page = ManifestPageRequest(entries=[_entry_wire()], page_digest=_SHA256)
    assert len(page.entries) == 1
    with pytest.raises(ValidationError):
        ManifestPageRequest.model_validate(
            {
                "entries": [_entry_wire() for _ in range(MAX_MANIFEST_PAGE_ENTRIES + 1)],
                "page_digest": _SHA256,
            }
        )
    with pytest.raises(ValidationError):
        ManifestPageRequest.model_validate({"entries": [], "page_digest": "not-a-digest"})


@pytest.mark.parametrize(
    ("model", "body"),
    [
        (
            ManifestFinalizeRequest,
            {"total_entry_count": -1, "final_digest": _SHA256},
        ),
        (ManifestCompleteRequest, {"final_digest": "uppercase" * 8}),
        (ManifestStartRequest, {"client_observation_generation": -2}),
    ],
)
def test_manifest_bodies_reject_out_of_range_values(model: type[Any], body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(body)


# --- boundary conversion ------------------------------------------------------------------------


def test_to_domain_entries_converts_strict_entries_exactly() -> None:
    wire = ManifestEntryRequest.model_validate(
        _entry_wire(known_source_id=str(uuid4()), known_version_id=str(uuid4()))
    )
    (entry,) = to_domain_entries([wire])
    assert entry.local_entry_id == "note-1"
    assert entry.known_source_id == wire.known_source_id
    assert entry.known_version_id == wire.known_version_id
    assert entry.normalized_locator == NormalizedLocator("notes/note.md")
    assert entry.fingerprint == SourceFingerprint(
        sha256=_SHA256, size_bytes=128, media_type="text/markdown"
    )
    assert entry.observation_generation == 4


def test_to_domain_entries_maps_nil_evidence_and_bad_grammar_to_page_invalid() -> None:
    nil_known = ManifestEntryRequest.model_validate(
        _entry_wire(known_source_id=str(UUID(int=0)))
    )
    with pytest.raises(DeviceSyncError) as raised:
        to_domain_entries([nil_known])
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_PAGE_INVALID

    bad_media = ManifestEntryRequest.model_validate(
        _entry_wire(
            fingerprint={"sha256": _SHA256, "size_bytes": 1, "media_type": "text/markdown; v=1"}
        )
    )
    with pytest.raises(DeviceSyncError) as raised_media:
        to_domain_entries([bad_media])
    assert raised_media.value.code is DeviceSyncErrorCode.MANIFEST_PAGE_INVALID

    bad_locator = ManifestEntryRequest.model_validate(
        _entry_wire(normalized_locator="notes\\note.md")
    )
    with pytest.raises(DeviceSyncError) as raised_locator:
        to_domain_entries([bad_locator])
    assert raised_locator.value.code is DeviceSyncErrorCode.MANIFEST_PAGE_INVALID


def test_digest_parsers_map_parse_failures_to_their_closed_codes() -> None:
    assert parse_page_digest(_SHA256).hexadecimal == _SHA256
    assert parse_final_digest(_SHA256).hexadecimal == _SHA256
    with pytest.raises(DeviceSyncError) as page_failure:
        parse_page_digest("0" * 63)
    assert page_failure.value.code is DeviceSyncErrorCode.MANIFEST_PAGE_INVALID
    with pytest.raises(DeviceSyncError) as final_failure:
        parse_final_digest("0" * 63)
    assert final_failure.value.code is DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH


# --- response renderers ------------------------------------------------------------------------


def _domain_event() -> DeviceSyncEvent:
    return DeviceSyncEvent(
        event_id=uuid4(),
        event_sequence=11,
        event_type=DeviceEventType.RENAMED,
        source_id=uuid4(),
        origin_device_id=uuid4(),
        base_version_id=uuid4(),
        current_version_id=uuid4(),
        base_fingerprint=SourceFingerprint(
            sha256=_SHA256, size_bytes=10, media_type="text/markdown"
        ),
        current_fingerprint=SourceFingerprint(
            sha256="b" * 64, size_bytes=20, media_type="text/markdown"
        ),
        prior_locator=NormalizedLocator("notes/old.md"),
        resulting_locator=NormalizedLocator("notes/new.md"),
        tombstone_id=None,
        committed_at=_COMMITTED_AT,
    )


def test_device_event_page_data_renders_exactly_the_safe_members() -> None:
    event = _domain_event()
    page = DeviceEventPage(
        acknowledged_sequence=5,
        page_checkpoint_sequence=12,
        delivered_through_sequence=11,
        events=(event,),
        has_more=True,
    )
    data = device_event_page_data(page)
    assert isinstance(data, DeviceEventPageData)
    assert data.acknowledged_sequence == 5
    assert data.page_checkpoint_sequence == 12
    assert data.delivered_through_sequence == 11
    assert data.has_more is True
    (rendered,) = data.events
    assert isinstance(rendered, DeviceSyncEventData)
    assert rendered.event_type is DeviceEventType.RENAMED
    assert rendered.base_fingerprint == SourceFingerprintData(
        sha256=_SHA256, size_bytes=10, media_type="text/markdown"
    )
    assert rendered.prior_locator == "notes/old.md"
    assert rendered.resulting_locator == "notes/new.md"
    assert rendered.tombstone_id is None
    rendered_json = data.model_dump(mode="json")
    assert "receipt" not in rendered_json
    assert "object_key" not in rendered_json


def test_cursor_and_run_receipt_renderers_carry_exactly_their_members() -> None:
    receipt = device_cursor_receipt_data(DeviceCursorReceipt(5, 11))
    assert isinstance(receipt, DeviceCursorReceiptData)
    assert receipt.acknowledged_sequence == 5
    assert receipt.delivered_through_sequence == 11

    run = manifest_run_receipt_data(
        ManifestRunReceipt(
            manifest_run_id=uuid4(),
            state=ManifestRunState.PLANNED,
            base_acknowledged_sequence=5,
            checkpoint_sequence=11,
            policy_revision_number=2,
            client_observation_generation=4,
            next_page_number=2,
            entry_count=9,
            expires_at=_EXPIRES_AT,
        )
    )
    assert isinstance(run, ManifestRunReceiptData)
    assert run.state is ManifestRunState.PLANNED
    assert run.policy_revision_number == 2
    assert run.entry_count == 9

    page = manifest_page_receipt_data(
        ManifestPageReceipt(
            manifest_run_id=uuid4(), page_number=1, accepted_entry_count=9, next_page_number=2
        )
    )
    assert isinstance(page, ManifestPageReceiptData)
    assert page.accepted_entry_count == 9
    assert page.next_page_number == 2


def test_manifest_action_page_renderer_projects_the_closed_action_shape() -> None:
    actions = (
        ManifestAction(
            action_index=0,
            action_kind=ManifestActionKind.UPLOAD,
            local_entry_id="note-1",
            source_id=uuid4(),
            source_version_id=uuid4(),
            source_locator_id=None,
            source_tombstone_id=None,
            reason=None,
        ),
        ManifestAction(
            action_index=1,
            action_kind=ManifestActionKind.CONFLICT,
            local_entry_id="note-2",
            source_id=None,
            source_version_id=None,
            source_locator_id=None,
            source_tombstone_id=None,
            reason=ManifestActionReason.IDENTITY_AMBIGUOUS,
        ),
    )
    data = manifest_action_page_data(
        ManifestActionPage(manifest_run_id=uuid4(), actions=actions, has_more=False)
    )
    assert isinstance(data, ManifestActionPageData)
    assert data.has_more is False
    first, second = data.actions
    assert isinstance(first, ManifestActionData)
    assert first.action_kind is ManifestActionKind.UPLOAD
    assert first.source_version_id is not None
    assert second.reason is ManifestActionReason.IDENTITY_AMBIGUOUS
