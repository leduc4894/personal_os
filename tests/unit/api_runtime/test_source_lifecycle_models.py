"""Strict source lifecycle wire models and the domain boundary conversion.

These tests pin the request and response model contracts of the Task 6 API
surface: every model is frozen and closed for extra fields, the body mirrors
the closed ``SourceLifecycleCommand`` operation-dependent field grammar
(``rename`` / ``move`` require both ``expected_locator`` and
``target_locator`` and reject ``tombstone_id``; ``delete`` requires
``expected_locator`` only; ``restore`` requires ``target_locator`` and
``tombstone_id``), ``policy_revision`` is a closed positive integer and the
opaque idempotency-key pattern matches the sources contract, and the
response renderer emits the strict ``SourceLifecycleCommitResult`` payload
with no fingerprint, locator text or fingerprint leaking into the envelope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

import pytest
from api_runtime.source_lifecycle_models import (
    SourceLifecycleCommitData,
    SourceLifecycleEventRequest,
    source_lifecycle_commit_data,
)
from pydantic import ValidationError

from personal_os.api_contracts import success_envelope
from personal_os.source_lifecycle.commands import (
    LifecycleOperation,
    LifecycleState,
    SourceLifecycleCommitResult,
)
from personal_os.source_locators import NormalizedLocator

EVENT_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000003")
SOURCE_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000002")
EXPECTED_VERSION_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000005")
TOMBSTONE_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000034")
RESULT_VERSION_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000099")

#: The canonical opaque idempotency-key spelling of the sources contract: a
#: printable non-whitespace ASCII string of 1-200 characters.
_IDEMPOTENCY_KEY: Final[str] = "lifecycle-rename-001"


def _rename_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "event_id": str(EVENT_ID),
        "idempotency_key": _IDEMPOTENCY_KEY,
        "source_id": str(SOURCE_ID),
        "operation": LifecycleOperation.RENAME.value,
        "expected_version_id": str(EXPECTED_VERSION_ID),
        "expected_locator": "notes/old.md",
        "target_locator": "notes/new.md",
        "tombstone_id": None,
        "policy_revision": 1,
        "client_timestamp": "2026-08-20T01:02:03Z",
    }
    body.update(overrides)
    return body


def _delete_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "event_id": str(EVENT_ID),
        "idempotency_key": _IDEMPOTENCY_KEY,
        "source_id": str(SOURCE_ID),
        "operation": LifecycleOperation.DELETE.value,
        "expected_version_id": str(EXPECTED_VERSION_ID),
        "expected_locator": "notes/drop.md",
        "target_locator": None,
        "tombstone_id": None,
        "policy_revision": 3,
        "client_timestamp": None,
    }
    body.update(overrides)
    return body


def _restore_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "event_id": str(EVENT_ID),
        "idempotency_key": _IDEMPOTENCY_KEY,
        "source_id": str(SOURCE_ID),
        "operation": LifecycleOperation.RESTORE.value,
        "expected_version_id": str(EXPECTED_VERSION_ID),
        "expected_locator": None,
        "target_locator": "notes/restored.md",
        "tombstone_id": str(TOMBSTONE_ID),
        "policy_revision": 5,
        "client_timestamp": "2026-08-20T01:02:03.000001Z",
    }
    body.update(overrides)
    return body


def test_rename_request_is_strict_and_carries_expected_and_target_locator() -> None:
    parsed = SourceLifecycleEventRequest.model_validate(_rename_body())
    assert parsed.event_id == EVENT_ID
    assert parsed.operation is LifecycleOperation.RENAME
    assert parsed.expected_locator == NormalizedLocator("notes/old.md")
    assert parsed.target_locator == NormalizedLocator("notes/new.md")
    assert parsed.tombstone_id is None
    assert parsed.policy_revision == 1
    assert parsed.client_timestamp == datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC)


def test_delete_request_accepts_expected_locator_only() -> None:
    parsed = SourceLifecycleEventRequest.model_validate(_delete_body())
    assert parsed.operation is LifecycleOperation.DELETE
    assert parsed.expected_locator == NormalizedLocator("notes/drop.md")
    assert parsed.target_locator is None
    assert parsed.tombstone_id is None
    assert parsed.client_timestamp is None


def test_restore_request_requires_target_locator_and_tombstone_id() -> None:
    parsed = SourceLifecycleEventRequest.model_validate(_restore_body())
    assert parsed.operation is LifecycleOperation.RESTORE
    assert parsed.expected_locator is None
    assert parsed.target_locator == NormalizedLocator("notes/restored.md")
    assert parsed.tombstone_id == TOMBSTONE_ID
    assert parsed.policy_revision == 5


def test_request_model_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(
            {**_rename_body(), "workspace_id": str(SOURCE_ID)}
        )


def test_request_model_rejects_workspace_or_device_selectors() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(
            {**_rename_body(), "workspace_id": str(SOURCE_ID)}
        )
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate({**_rename_body(), "device_id": str(SOURCE_ID)})


def test_rename_rejects_tombstone_id() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(_rename_body(tombstone_id=str(TOMBSTONE_ID)))


def test_rename_rejects_missing_target_locator() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(_rename_body(target_locator=None))


def test_rename_rejects_missing_expected_locator() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(_rename_body(expected_locator=None))


def test_rename_rejects_equal_expected_and_target_locator() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(
            _rename_body(expected_locator="notes/same.md", target_locator="notes/same.md")
        )


def test_delete_rejects_target_locator() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(
            _delete_body(target_locator="notes/still-here.md")
        )


def test_delete_rejects_tombstone_id() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(_delete_body(tombstone_id=str(TOMBSTONE_ID)))


def test_restore_rejects_expected_locator() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(_restore_body(expected_locator="notes/old.md"))


def test_restore_rejects_missing_tombstone_id() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(_restore_body(tombstone_id=None))


def test_restore_rejects_missing_target_locator() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(_restore_body(target_locator=None))


def test_policy_revision_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(_rename_body(policy_revision=0))
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(_rename_body(policy_revision=-1))


def test_event_id_must_be_uuidv7() -> None:
    # UUIDv4 raises the version-7 invariant in the domain command; the wire
    # model surfaces it as the closed Pydantic error path. Use a known
    # non-v7 UUID (uuid4 fixture value).
    non_v7 = UUID("00000000-0000-4000-8000-000000000000")
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(_rename_body(event_id=str(non_v7)))


def test_client_timestamp_is_optional_and_accepts_rfc3339_with_subseconds() -> None:
    parsed = SourceLifecycleEventRequest.model_validate(
        _rename_body(client_timestamp="2026-08-20T01:02:03.000001Z")
    )
    assert parsed.client_timestamp == datetime(2026, 8, 20, 1, 2, 3, 0, tzinfo=UTC).replace(
        microsecond=1
    )


def test_client_timestamp_rejects_naive_string() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(
            _rename_body(client_timestamp="2026-08-20T01:02:03")
        )


def test_idempotency_key_must_match_the_sources_pattern() -> None:
    with pytest.raises(ValidationError):
        SourceLifecycleEventRequest.model_validate(_rename_body(idempotency_key="bad key"))


def test_response_data_emits_only_safe_receipt_members() -> None:
    result = SourceLifecycleCommitResult(
        source_id=SOURCE_ID,
        source_version_id=RESULT_VERSION_ID,
        event_id=EVENT_ID,
        event_sequence=7,
        state=LifecycleState.ACTIVE,
        tombstone_id=None,
        resulting_locator=NormalizedLocator("notes/new.md"),
        committed_at=datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )
    rendered = source_lifecycle_commit_data(result)
    assert set(rendered.model_dump(mode="json", exclude_unset=True)) == {
        "source_id",
        "source_version_id",
        "event_id",
        "event_sequence",
        "state",
        "tombstone_id",
        "resulting_locator",
        "committed_at",
    }


def test_response_data_state_enum_is_closed() -> None:
    delete_result = SourceLifecycleCommitResult(
        source_id=SOURCE_ID,
        source_version_id=RESULT_VERSION_ID,
        event_id=EVENT_ID,
        event_sequence=2,
        state=LifecycleState.DELETED,
        tombstone_id=TOMBSTONE_ID,
        resulting_locator=None,
        committed_at=datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )
    rendered = source_lifecycle_commit_data(delete_result)
    assert rendered.state == LifecycleState.DELETED.value
    assert rendered.tombstone_id == TOMBSTONE_ID
    assert rendered.resulting_locator is None


def test_response_data_is_closed_against_extra_fields() -> None:
    data = SourceLifecycleCommitData(
        source_id=SOURCE_ID,
        source_version_id=RESULT_VERSION_ID,
        event_id=EVENT_ID,
        event_sequence=1,
        state=LifecycleState.ACTIVE.value,
        tombstone_id=None,
        resulting_locator="notes/new.md",
        committed_at=datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )
    with pytest.raises(ValidationError):
        SourceLifecycleCommitData.model_validate(
            {**data.model_dump(), "workspace_id": str(SOURCE_ID)}
        )


def test_success_envelope_wraps_the_rendered_data() -> None:
    data = SourceLifecycleCommitData(
        source_id=SOURCE_ID,
        source_version_id=RESULT_VERSION_ID,
        event_id=EVENT_ID,
        event_sequence=1,
        state=LifecycleState.ACTIVE.value,
        tombstone_id=None,
        resulting_locator="notes/new.md",
        committed_at=datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )
    envelope = success_envelope(request_id=EVENT_ID, data=data)
    rendered = envelope.model_dump(mode="json", exclude_unset=True)
    assert rendered["data"]["source_id"] == str(SOURCE_ID)
    assert rendered["data"]["state"] == "active"
    assert rendered["error"] is None
