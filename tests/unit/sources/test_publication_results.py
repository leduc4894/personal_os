"""Source version publication result contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from personal_os.object_storage import ContentDigest
from personal_os.sources import PublicationOutcome, SourceVersionPublicationResult

DIGEST = ContentDigest.parse("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
UTC_PLUS_TWO = timezone(timedelta(hours=2))


def _result(**overrides: object) -> SourceVersionPublicationResult:
    values: dict[str, object] = {
        "source_id": uuid4(),
        "source_version_id": uuid4(),
        "content_version": 1,
        "event_id": uuid4(),
        "event_sequence": 1,
        "content_digest": DIGEST,
        "outcome": PublicationOutcome.PUBLISHED,
        "committed_at": datetime(2026, 8, 14, 12, 30, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return SourceVersionPublicationResult(**values)  # type: ignore[arg-type]


def test_publication_outcome_is_closed_over_published_and_no_change() -> None:
    assert {member.value for member in PublicationOutcome} == {"published", "no_change"}


def test_result_keeps_canonical_fields() -> None:
    source_id = uuid4()
    result = _result(source_id=source_id, outcome=PublicationOutcome.NO_CHANGE)
    assert result.source_id == source_id
    assert result.content_version == 1
    assert result.event_sequence == 1
    assert result.content_digest == DIGEST
    assert result.outcome is PublicationOutcome.NO_CHANGE
    assert result.committed_at.tzinfo is UTC


@pytest.mark.parametrize("field_name", ["source_id", "source_version_id", "event_id"])
def test_rejects_nil_uuids(field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        _result(**{field_name: UUID(int=0)})


@pytest.mark.parametrize("content_version", [0, -1])
def test_rejects_nonpositive_content_version(content_version: int) -> None:
    with pytest.raises(ValueError, match="content_version"):
        _result(content_version=content_version)


@pytest.mark.parametrize("event_sequence", [0, -1])
def test_rejects_nonpositive_event_sequence(event_sequence: int) -> None:
    with pytest.raises(ValueError, match="event_sequence"):
        _result(event_sequence=event_sequence)


def test_rejects_naive_committed_at() -> None:
    with pytest.raises(ValueError, match="committed_at"):
        _result(committed_at=datetime(2026, 8, 14, 12, 30, 0))


def test_normalizes_aware_committed_at_to_utc() -> None:
    result = _result(committed_at=datetime(2026, 8, 14, 14, 30, 0, tzinfo=UTC_PLUS_TWO))
    assert result.committed_at == datetime(2026, 8, 14, 12, 30, 0, tzinfo=UTC)
    assert result.committed_at.tzinfo is UTC


def test_result_is_frozen() -> None:
    result = _result()
    with pytest.raises(FrozenInstanceError):
        result.content_version = 2  # type: ignore[misc]
