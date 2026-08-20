"""Golden-byte and exclusion tests for canonical source request fingerprints."""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID

from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.source_locators import NormalizedLocator
from personal_os.sources import (
    ActorKind,
    CreateSourceVersion,
    IdempotencyKey,
    SourceActor,
    SourceTitle,
    SourceType,
    UpdateSourceVersion,
)
from personal_os.sources.fingerprint import (
    RequestFingerprint,
    compute_request_fingerprint,
)

FIXTURE_PATH = (
    Path(__file__).parents[2] / "fixtures" / "source_publication" / "fingerprint_golden.json"
)
UTC_PLUS_TWO = timezone(timedelta(hours=2))
DEFAULT_IDEMPOTENCY_KEY = IdempotencyKey("publish-001")


def _load_fixture() -> dict[str, dict[str, object]]:
    with FIXTURE_PATH.open("r", encoding="utf-8") as fixture_file:
        return cast(dict[str, dict[str, object]], json.load(fixture_file))


def _expected_object(fixture: Mapping[str, object]) -> ExpectedObject:
    return ExpectedObject(
        content_digest=ContentDigest.parse(cast(str, fixture["content_sha256"])),
        size_bytes=cast(int, fixture["content_size_bytes"]),
        media_type=CanonicalMediaType.parse(cast(str, fixture["media_type"])),
    )


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(cast(str, value).replace("Z", "+00:00"))


def _actor(fixture: Mapping[str, object]) -> SourceActor:
    return SourceActor(
        actor_kind=ActorKind(cast(str, fixture["actor_kind"])),
        actor_id=UUID(cast(str, fixture["actor_id"])),
    )


def _create_command(
    fixture: Mapping[str, object],
    *,
    idempotency_key: IdempotencyKey = DEFAULT_IDEMPOTENCY_KEY,
    client_timestamp: datetime | None | object = ...,
    initial_locator: NormalizedLocator | None = None,
) -> CreateSourceVersion:
    timestamp = (
        _parse_timestamp(fixture["client_timestamp"])
        if client_timestamp is ...
        else cast(datetime | None, client_timestamp)
    )
    return CreateSourceVersion(
        workspace_id=UUID(cast(str, fixture["workspace_id"])),
        source_id=UUID(cast(str, fixture["source_id"])),
        event_id=UUID(cast(str, fixture["event_id"])),
        idempotency_key=idempotency_key,
        source_type=SourceType(cast(str, fixture["source_type"])),
        title=SourceTitle(cast(str, fixture["title"])),
        actor=_actor(fixture),
        expected_object=_expected_object(fixture),
        client_timestamp=timestamp,
        initial_locator=initial_locator,
    )


def _update_command(
    fixture: Mapping[str, object],
    *,
    idempotency_key: IdempotencyKey = DEFAULT_IDEMPOTENCY_KEY,
) -> UpdateSourceVersion:
    return UpdateSourceVersion(
        workspace_id=UUID(cast(str, fixture["workspace_id"])),
        source_id=UUID(cast(str, fixture["source_id"])),
        event_id=UUID(cast(str, fixture["event_id"])),
        idempotency_key=idempotency_key,
        base_version_id=UUID(cast(str, fixture["base_version_id"])),
        actor=_actor(fixture),
        expected_object=_expected_object(fixture),
        client_timestamp=_parse_timestamp(fixture["client_timestamp"]),
    )


def test_create_fixture_matches_golden_request_fingerprint() -> None:
    fixture = _load_fixture()["create"]

    fingerprint = compute_request_fingerprint(_create_command(fixture))

    assert fingerprint == RequestFingerprint.parse(
        cast(str, fixture["expected_request_fingerprint"])
    )


def test_update_fixture_matches_golden_request_fingerprint() -> None:
    fixture = _load_fixture()["update"]

    fingerprint = compute_request_fingerprint(_update_command(fixture))

    assert fingerprint == RequestFingerprint.parse(
        cast(str, fixture["expected_request_fingerprint"])
    )


def test_create_with_initial_locator_matches_v2_golden_request_fingerprint() -> None:
    fixture = _load_fixture()["create_v2"]

    fingerprint = compute_request_fingerprint(
        _create_command(
            fixture,
            initial_locator=NormalizedLocator(cast(str, fixture["initial_locator"])),
        )
    )

    assert fingerprint == RequestFingerprint.parse(
        cast(str, fixture["expected_request_fingerprint"])
    )


def test_fingerprint_signature_excludes_transport_and_generated_fields() -> None:
    parameters = list(inspect.signature(compute_request_fingerprint).parameters)

    assert parameters == ["command"]


def test_idempotency_key_cannot_change_request_fingerprint() -> None:
    fixture = _load_fixture()["create"]
    first = compute_request_fingerprint(
        _create_command(fixture, idempotency_key=IdempotencyKey("publish-001"))
    )
    second = compute_request_fingerprint(
        _create_command(fixture, idempotency_key=IdempotencyKey("publish-002"))
    )

    assert first == second


def test_equivalent_offset_timestamp_yields_same_fingerprint() -> None:
    fixture = _load_fixture()["create"]
    local_timestamp = datetime(2026, 8, 14, 5, 11, 12, 123456, tzinfo=UTC_PLUS_TWO)

    fingerprint = compute_request_fingerprint(
        _create_command(fixture, client_timestamp=local_timestamp)
    )

    assert fingerprint == RequestFingerprint.parse(
        cast(str, fixture["expected_request_fingerprint"])
    )


def test_absent_client_timestamp_is_fingerprinted_as_null_member() -> None:
    fixture = _load_fixture()["create"]

    without_timestamp = compute_request_fingerprint(_create_command(fixture, client_timestamp=None))
    with_timestamp = compute_request_fingerprint(_create_command(fixture))

    assert without_timestamp != with_timestamp


def test_distinct_requests_yield_distinct_fingerprints() -> None:
    fixture = _load_fixture()
    create_fingerprint = compute_request_fingerprint(_create_command(fixture["create"]))
    update_fingerprint = compute_request_fingerprint(_update_command(fixture["update"]))
    retitled = dict(fixture["create"])
    retitled["title"] = "Ghi chú khác"

    retitled_fingerprint = compute_request_fingerprint(_create_command(retitled))

    assert create_fingerprint != update_fingerprint
    assert create_fingerprint != retitled_fingerprint
