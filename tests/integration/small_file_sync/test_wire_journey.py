"""End-to-end wire journeys over the real application factory (spec 10).

These fixtures drive the two closed sync routes through the composed
application — real route stack, real request correlation and security
posture, real domain service — with wire bodies shaped exactly like the
plugin client's hand-mirrored requests. The journeys pin the cross-boundary
behaviors the plugin depends on: a create commits exactly once and answers
the lost-response replay with the frozen receipt; the same journal identity
re-preflight after commit replays exactly without a second publication; a
stale update base answers the durable ``conflict`` outcome without a
reservation; a current base whose digest equals the declared digest
answers the frozen ``no_change`` receipt; and a device that suspends past
the operation deadline mid-upload resumes through a same-identity
re-preflight that re-reserves the expired operation so the event still
completes with exactly one publication.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Final
from uuid import UUID, uuid4

from tests.integration.small_file_sync.conftest import SmallFileWireHarness

from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.sources.commands import SourceType
from personal_os.sources.reading import CanonicalSourceReference

_CONTENT: bytes = b"# small-file wire journey content\n"
_CONTENT_DIGEST: str = sha256(_CONTENT).hexdigest()
_EDITED_CONTENT: bytes = _CONTENT + b"with a later local edit\n"
_EDITED_CONTENT_DIGEST: str = sha256(_EDITED_CONTENT).hexdigest()
_MEDIA_TYPE: str = "text/markdown"
_LOCATOR: str = "notes/journey-note.md"
_CURRENT_BASE_COMMITTED_AT: Final[datetime] = datetime(2026, 8, 18, 9, 30, 0, tzinfo=UTC)

#: Frozen reservation and resume moments of the suspend journey: the device
#: suspends two hours past the fifteen-minute operation deadline, then the
#: resume pass re-preflights the same journal identity.
_SUSPEND_RESERVATION_AT: Final[datetime] = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
_SUSPEND_RESUME_AT: Final[datetime] = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

#: The exact members a terminal receipt may carry on the wire (spec 10.3).
TERMINAL_RESULT_MEMBERS: Final[frozenset[str]] = frozenset(
    {"result_kind", "source_id", "source_version_id", "content_version", "committed_at"}
)


def plugin_create_body(*, locator: str = _LOCATOR) -> dict[str, Any]:
    """One create preflight body shaped exactly like the plugin client's."""

    return {
        "event_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "operation": "create",
        "local_file_id": str(uuid4()),
        "source_id": None,
        "base_version_id": None,
        "normalized_locator": locator,
        "sha256": _CONTENT_DIGEST,
        "size_bytes": len(_CONTENT),
        "media_type": _MEDIA_TYPE,
        "policy_revision": 3,
    }


def plugin_update_body(
    *,
    source_id: str,
    base_version_id: str,
    digest: str = _CONTENT_DIGEST,
    size_bytes: int = len(_CONTENT),
) -> dict[str, Any]:
    """One update preflight body shaped exactly like the plugin client's."""

    return {
        "event_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "operation": "update",
        "local_file_id": str(uuid4()),
        "source_id": source_id,
        "base_version_id": base_version_id,
        "normalized_locator": _LOCATOR,
        "sha256": digest,
        "size_bytes": size_bytes,
        "media_type": _MEDIA_TYPE,
        "policy_revision": 3,
    }


def data_of(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return dict(response.json()["data"])


def single_part_token(harness: SmallFileWireHarness, body: dict[str, Any]) -> str:
    data = data_of(harness.preflight(body))
    assert data["outcome"] == "single_part_upload", data
    return str(data["operation_id"])


def _current_reference(
    body: dict[str, Any],
    *,
    source_version_id: UUID | None = None,
    digest: str | None = None,
) -> CanonicalSourceReference:
    """The current pointer view of the update's source.

    By default the reference carries the update's own declared digest (the
    matching-base no-change shape); pass ``digest`` to model a current base
    committed with different bytes.
    """

    source_id = UUID(str(body["source_id"]))
    base_version_id = UUID(str(body["base_version_id"]))
    return CanonicalSourceReference(
        workspace_id=uuid4(),
        source_id=source_id,
        source_version_id=source_version_id or base_version_id,
        content_version=2,
        source_type=SourceType.MARKDOWN,
        expected_object=ExpectedObject(
            content_digest=ContentDigest.parse(digest or str(body["sha256"])),
            size_bytes=int(body["size_bytes"]),
            media_type=CanonicalMediaType.parse(str(body["media_type"])),
        ),
        committed_at=_CURRENT_BASE_COMMITTED_AT,
    )


def test_create_journey_commits_once_and_replays_the_lost_response(
    offline_harness: SmallFileWireHarness,
) -> None:
    """The dropped-response journey: one publication, one frozen receipt.

    The client uploads, the response is lost, and the client repeats the
    upload with the same operation token: the server must answer the frozen
    terminal receipt byte-for-byte and publish nothing new.
    """

    harness = offline_harness
    body = plugin_create_body()
    token = single_part_token(harness, body)

    committed = data_of(harness.upload(token, _CONTENT))
    assert set(committed) == TERMINAL_RESULT_MEMBERS
    assert committed["result_kind"] == "committed"
    assert committed["content_version"] == 1
    assert harness.sync_state.publication_commits == 1

    replayed = data_of(harness.upload(token, _CONTENT))
    assert replayed == committed
    assert harness.sync_state.publication_commits == 1
    assert harness.sync_state.reservation_count == 1


def test_same_identity_repreflight_after_commit_replays_exactly(
    offline_harness: SmallFileWireHarness,
) -> None:
    """The reconnect journey: the same event re-preflights onto its receipt."""

    harness = offline_harness
    body = plugin_create_body()
    token = single_part_token(harness, body)
    committed = data_of(harness.upload(token, _CONTENT))

    replay = data_of(harness.preflight(body))
    assert replay["outcome"] == "committed_replay"
    assert set(replay) == {"outcome", "result"}
    assert set(replay["result"]) == TERMINAL_RESULT_MEMBERS
    assert replay["result"] == committed
    # The replay allocates neither another operation nor another publication.
    assert harness.sync_state.reservation_count == 1
    assert harness.sync_state.publication_commits == 1


def test_stale_update_base_answers_conflict_without_a_reservation(
    offline_harness: SmallFileWireHarness,
) -> None:
    """A base that is no longer current never opens an upload."""

    harness = offline_harness
    body = plugin_update_body(source_id=str(uuid4()), base_version_id=str(uuid4()))
    # The current pointer names a DIFFERENT version than the declared base.
    harness.sync_state.current_reference = _current_reference(body, source_version_id=uuid4())

    response = harness.preflight(body)
    assert response.status_code == 200, response.text
    assert dict(response.json()["data"]) == {"outcome": "conflict"}
    assert harness.sync_state.reservation_count == 0
    assert harness.sync_state.publication_commits == 0


def test_matching_update_base_freezes_the_no_change_receipt(
    offline_harness: SmallFileWireHarness,
) -> None:
    """A current base with identical bytes freezes the safe no-op receipt."""

    harness = offline_harness
    body = plugin_update_body(source_id=str(uuid4()), base_version_id=str(uuid4()))
    harness.sync_state.current_reference = _current_reference(body)

    first = data_of(harness.preflight(body))
    assert first["outcome"] == "no_change"
    assert set(first) == {"outcome", "result"}
    assert first["result"]["result_kind"] == "no_change"

    # A reconnect replays the frozen no-change receipt exactly, with no
    # second reservation and no publication.
    replay = data_of(harness.preflight(body))
    assert replay == first
    assert harness.sync_state.reservation_count == 1
    assert harness.sync_state.publication_commits == 0


def test_create_then_update_journey_advances_the_committed_base(
    offline_harness: SmallFileWireHarness,
) -> None:
    """The plugin's create-then-edit flow: the update rides the create receipt.

    The create commits and hands back the canonical source and base version;
    the very next update preflight carries exactly those identities, exactly
    as the queue driver derives them from the local file mapping.
    """

    harness = offline_harness
    create_body = plugin_create_body()
    token = single_part_token(harness, create_body)
    committed = data_of(harness.upload(token, _CONTENT))

    update_body = plugin_update_body(
        source_id=str(committed["source_id"]),
        base_version_id=str(committed["source_version_id"]),
        digest=_EDITED_CONTENT_DIGEST,
        size_bytes=len(_EDITED_CONTENT),
    )
    # The current pointer still names the create's version holding the
    # create's bytes; the declared bytes differ, so the update opens a
    # single-part upload.
    harness.sync_state.current_reference = _current_reference(update_body, digest=_CONTENT_DIGEST)
    update_data = data_of(harness.preflight(update_body))
    assert update_data["outcome"] == "single_part_upload"
    assert set(update_data) == {"outcome", "operation_id", "expires_at"}

    updated = data_of(harness.upload(str(update_data["operation_id"]), _EDITED_CONTENT))
    assert updated["result_kind"] == "committed"
    assert updated["content_version"] == 2
    assert updated["source_id"] == committed["source_id"]
    assert harness.sync_state.publication_commits == 2


def test_resume_after_expiry_re_reserves_and_publishes_exactly_once(
    offline_harness: SmallFileWireHarness,
) -> None:
    """The suspend journey: an expired pending operation never wedges.

    The preflight succeeds, the device suspends past the fifteen-minute
    operation deadline before the upload completes (mobile backgrounding,
    sleep, app close), and the resume pass re-preflights the same journal
    identity. The old token stays refused at receive time (410 expired),
    the server re-reserves the same operation row with a fresh token, the
    resumed upload commits, and exactly one publication exists — the frozen
    receipt then replays with no second publish.
    """

    harness = offline_harness
    harness.sync_state.now = _SUSPEND_RESERVATION_AT
    body = plugin_create_body()
    suspended_token = single_part_token(harness, body)

    harness.sync_state.now = _SUSPEND_RESUME_AT
    expired = harness.upload(suspended_token, _CONTENT)
    assert expired.status_code == 410, expired.text
    assert expired.json()["error"]["code"] == "small_file_operation_expired"

    resumed = data_of(harness.preflight(body))
    assert resumed["outcome"] == "single_part_upload"
    assert set(resumed) == {"outcome", "operation_id", "expires_at"}
    resumed_token = str(resumed["operation_id"])
    assert resumed_token != suspended_token

    committed = data_of(harness.upload(resumed_token, _CONTENT))
    assert committed["result_kind"] == "committed"
    assert committed["content_version"] == 1
    assert harness.sync_state.publication_commits == 1
    assert harness.sync_state.reservation_count == 1

    replay = data_of(harness.preflight(body))
    assert replay["outcome"] == "committed_replay"
    assert replay["result"] == committed
    assert harness.sync_state.publication_commits == 1
