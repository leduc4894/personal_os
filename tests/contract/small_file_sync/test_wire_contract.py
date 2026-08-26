"""Cross-boundary wire contract of the small-file sync surfaces (spec 10, 12).

One corpus, two replays, one hash — the exclusion-policy cross-language
precedent applied to the two sync surfaces. The golden corpus under
``tests/fixtures/small_file_sync/wire-golden.json`` carries the canonical
envelope of every preflight outcome, the committed terminal result and the
closed error envelopes with their registered statuses plus the plugin-side
closed landing of each shape, and — since the reconciliation child's task 9
— three device-sync error envelopes (cursor gap, manifest policy advance,
download integrity) replayed through the hand-mirrored device client. This
gate pins the corpus hash, proves the Obsidian plugin's vitest replay reads
exactly these bytes and passes them through the real hand-mirrored clients,
and replays every route-reachable entry against the real application
factory so the served envelopes and the corpus cannot drift apart. Five
entries are unreachable through the served routes of this harness by design
— the defensive ``small_file_upload_state_invalid`` mapping (a committed
identity always replays its frozen receipt first), the typed create-time
``source_locator_conflict`` (produced only by the durable publication
store's guarded locator pre-check, which the offline harness double does not
model), and the three device-sync entries that ride the reconciliation
child's own route family — and stay corpus-only plugin-side coverage.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from tests.integration.small_file_sync.conftest import (
    SmallFileWireHarness,
    exchange_device_credential,
    excluding_folder_rule,
    maximum_size_rule,
    offline_wire_harness,
    policy_wire_harness,
    revoke_device_through_admin_route,
)

from personal_os.api_contracts.errors import HTTP_ERROR_STATUSES
from personal_os.error_contracts.codes import ERROR_DEFINITIONS, ErrorCode
from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.small_file_sync.contracts import MAX_SINGLE_PART_FILE_SIZE_BYTES
from personal_os.sources.commands import SourceType
from personal_os.sources.reading import CanonicalSourceReference

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
FIXTURE_PATH: Final[Path] = (
    REPO_ROOT / "tests" / "fixtures" / "small_file_sync" / "wire-golden.json"
)


@pytest.fixture
def offline_harness() -> Iterator[SmallFileWireHarness]:
    """One offline-graph harness over the shared integration composition."""

    with offline_wire_harness() as harness:
        yield harness


@pytest.fixture
def policy_harness() -> Iterator[SmallFileWireHarness]:
    """One serve-shaped policy-graph harness over the shared composition."""

    with policy_wire_harness() as harness:
        yield harness


#: The cross-language contract hash: both language replays consume these
#: exact bytes; changing the corpus means updating this registry in the same
#: commit.
WIRE_GOLDEN_SHA256: Final[str] = "25a165179cefc77578593554ee15967495762309ab79ea1377f633f033ab85c3"

#: The TypeScript replay suite that must read the fixture file.
TS_REPLAY_SOURCE: Final[str] = "apps/obsidian-plugin/src/journal/sync-wire-contract.test.ts"

_CONTENT: Final[bytes] = b"# shared wire-contract corpus content\n"
_CONTENT_DIGEST: Final[str] = sha256(_CONTENT).hexdigest()
_MEDIA_TYPE: Final[str] = "text/markdown"
_CURRENT_BASE_COMMITTED_AT: Final[datetime] = datetime(2026, 8, 18, 0, 0, 0, tzinfo=UTC)

#: The corpus entries the served routes of this harness can never produce: a
#: committed identity answers its frozen replay before any state transition
#: (``small_file_upload_state_invalid`` exists only as the plugin's defensive
#: mapping), and the typed create-time locator conflict is produced only by
#: the durable publication store's guarded pre-check — the harness's offline
#: publication double models no active-locator unique index — so
#: ``content_source_locator_conflict`` stays corpus/plugin-side coverage while
#: its registered status mapping is pinned by the API error-contract suite.
#: The three device-sync entries ride the device-sync route family of the
#: reconciliation child, not this harness's two small-file routes, so they
#: stay corpus/plugin-side coverage too; the device-sync route replay owns
#: their served envelopes.
_ROUTE_UNREACHABLE_ENTRIES: Final[frozenset[str]] = frozenset(
    {
        "error_small_file_upload_state_invalid",
        "content_source_locator_conflict",
        "device_error_cursor_gap",
        "device_error_manifest_policy_advanced",
        "device_error_download_integrity_failed",
    }
)

#: The device-sync surfaces the corpus replays through the hand-mirrored
#: device client (task 9 of the reconciliation child).
_DEVICE_SYNC_SURFACES: Final[frozenset[str]] = frozenset(
    {"device_events", "manifest_finalize", "device_download"}
)


def _entries() -> list[dict[str, Any]]:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert document["contract"] == "small_file_sync_wire_golden/v1"
    entries = document["entries"]
    assert isinstance(entries, list) and entries
    return list(entries)


def _create_body(
    *,
    locator: str = "notes/wire-contract.md",
    size_bytes: int = len(_CONTENT),
    sha256_text: str | None = None,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "operation": "create",
        "local_file_id": str(uuid4()),
        "source_id": None,
        "base_version_id": None,
        "normalized_locator": locator,
        "sha256": sha256_text or _CONTENT_DIGEST,
        "size_bytes": size_bytes,
        "media_type": _MEDIA_TYPE,
        "policy_revision": 2,
    }


def _update_body(*, source_id: str, base_version_id: str) -> dict[str, Any]:
    return {
        **_create_body(),
        "operation": "update",
        "source_id": source_id,
        "base_version_id": base_version_id,
    }


def _seeded_current_base(
    harness: SmallFileWireHarness, body: dict[str, Any], *, current_version_id: UUID | None = None
) -> None:
    """Point the current-source view at the update's source.

    By default the current version IS the declared base (the matching-base
    shape); pass ``current_version_id`` to model a base that went stale.
    """

    harness.sync_state.current_reference = CanonicalSourceReference(
        workspace_id=uuid4(),
        source_id=UUID(str(body["source_id"])),
        source_version_id=current_version_id or UUID(str(body["base_version_id"])),
        content_version=3,
        source_type=SourceType.MARKDOWN,
        expected_object=ExpectedObject(
            content_digest=ContentDigest.parse(str(body["sha256"])),
            size_bytes=int(body["size_bytes"]),
            media_type=CanonicalMediaType.parse(str(body["media_type"])),
        ),
        committed_at=_CURRENT_BASE_COMMITTED_AT,
    )


def _envelope_of(response: Any) -> dict[str, Any]:
    return dict(response.json())


def _single_part_upload(harness: SmallFileWireHarness, body: dict[str, Any]) -> str:
    response = harness.preflight(body)
    assert response.status_code == 200, response.text
    data = dict(response.json()["data"])
    assert data["outcome"] == "single_part_upload", data
    return str(data["operation_id"])


# --- the registry and the TypeScript replay -------------------------------------------


def test_the_wire_corpus_is_registered_and_its_hash_matches() -> None:
    actual = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert actual == WIRE_GOLDEN_SHA256, (
        "the small-file sync wire corpus drifted from the pinned cross-language "
        "hash; regenerate both language replays and update this registry together"
    )


def test_the_typescript_replay_reads_exactly_the_registered_corpus() -> None:
    source_path = REPO_ROOT / TS_REPLAY_SOURCE
    assert source_path.is_file(), f"the TypeScript replay surface {source_path} is missing"
    referenced = set(re.findall(r"(wire-golden\.json)", source_path.read_text(encoding="utf-8")))
    assert referenced == {"wire-golden.json"}, (
        "the TypeScript replay no longer reads the registered wire corpus; the "
        "cross-language contract is broken"
    )


def test_the_typescript_replay_of_the_corpus_passes() -> None:
    """Execute the plugin's vitest replay of the shared corpus.

    The Python replay below runs in-process; this gate proves the other
    language passes the identical bytes in the same run. A missing pnpm
    fails the gate — the cross-language contract never skips.
    """

    pnpm = shutil.which("pnpm")
    if pnpm is None:
        pytest.fail("pnpm is required to execute the TypeScript replay of the wire corpus")
    command = [
        "pnpm",
        "--filter",
        "@workspace/obsidian-plugin",
        "exec",
        "vitest",
        "run",
        "src/journal/sync-wire-contract.test.ts",
    ]
    if sys.platform == "win32":
        command = ["cmd.exe", "/c", pnpm, *command[1:]]
    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        "the TypeScript replay of the small-file sync wire corpus failed:\n"
        f"{completed.stdout}\n{completed.stderr}"
    )


# --- the served-envelope replay --------------------------------------------------------


def _offline_drivers(offline_harness: SmallFileWireHarness) -> dict[str, Callable[[], Any]]:
    """Route drivers over the offline graph, one per route-reachable entry."""

    def single_part_upload() -> Any:
        return offline_harness.preflight(_create_body())

    def committed_replay() -> Any:
        body = _create_body()
        token = _single_part_upload(offline_harness, body)
        assert offline_harness.upload(token, _CONTENT).status_code == 200
        return offline_harness.preflight(body)

    def no_change() -> Any:
        body = _update_body(source_id=str(uuid4()), base_version_id=str(uuid4()))
        _seeded_current_base(offline_harness, body)
        return offline_harness.preflight(body)

    def conflict() -> Any:
        body = _update_body(source_id=str(uuid4()), base_version_id=str(uuid4()))
        _seeded_current_base(offline_harness, body, current_version_id=uuid4())
        return offline_harness.preflight(body)

    def upload_committed() -> Any:
        token = _single_part_upload(offline_harness, _create_body())
        return offline_harness.upload(token, _CONTENT)

    def size_limit() -> Any:
        return offline_harness.preflight(
            _create_body(size_bytes=MAX_SINGLE_PART_FILE_SIZE_BYTES + 1, sha256_text="0" * 64)
        )

    def integrity_failed() -> Any:
        token = _single_part_upload(offline_harness, _create_body())
        return offline_harness.upload(token, b"# entirely different corpus bytes\n")

    def not_found() -> Any:
        return offline_harness.upload("a" * 43, _CONTENT)

    def expired() -> Any:
        offline_harness.sync_state.now = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
        token = _single_part_upload(offline_harness, _create_body())
        offline_harness.sync_state.now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
        return offline_harness.upload(token, _CONTENT)

    def identity_mismatch() -> Any:
        token = _single_part_upload(offline_harness, _create_body())
        foreign = exchange_device_credential(offline_harness.client, device_name="Foreign")
        return offline_harness.upload(token, _CONTENT, credential=foreign.access_credential)

    def credential_invalid() -> Any:
        return offline_harness.client.post(
            "/api/sync/journal-events/preflight", json=_create_body()
        )

    def device_revoked() -> Any:
        revoked = exchange_device_credential(offline_harness.client, device_name="Revoked")
        revoke_device_through_admin_route(offline_harness.client, revoked)
        return offline_harness.upload("a" * 43, _CONTENT, credential=revoked.access_credential)

    return {
        "preflight_single_part_upload": single_part_upload,
        "preflight_committed_replay": committed_replay,
        "preflight_no_change": no_change,
        "preflight_conflict": conflict,
        "upload_committed": upload_committed,
        "error_small_file_size_limit_exceeded": size_limit,
        "error_small_file_content_integrity_failed": integrity_failed,
        "error_small_file_operation_not_found": not_found,
        "error_small_file_operation_expired": expired,
        "error_small_file_operation_identity_mismatch": identity_mismatch,
        "error_device_credential_invalid": credential_invalid,
        "error_device_revoked": device_revoked,
    }


def _policy_drivers(policy_harness: SmallFileWireHarness) -> dict[str, Callable[[], Any]]:
    def excluded() -> Any:
        assert policy_harness.snapshot_source is not None
        policy_harness.snapshot_source.publish_rules((excluding_folder_rule("notes/private"),))
        return policy_harness.preflight(_create_body(locator="notes/private/secret.md"))

    def policy_denied() -> Any:
        assert policy_harness.snapshot_source is not None
        body = _create_body()
        token = _single_part_upload(policy_harness, body)
        policy_harness.snapshot_source.publish_rules((maximum_size_rule(8),))
        return policy_harness.upload(token, _CONTENT)

    return {
        "preflight_excluded": excluded,
        "error_exclusion_policy_denied": policy_denied,
    }


def _assert_envelope_matches_entry(entry: dict[str, Any], response: Any) -> None:
    golden = json.loads(str(entry["body_text"]))
    live = _envelope_of(response)
    assert response.status_code == entry["status"], entry["name"]
    if golden["data"] is not None:
        assert live["data"] is not None, entry["name"]
        assert set(live["data"]) == set(golden["data"]), entry["name"]
        if "outcome" in golden["data"]:
            assert live["data"]["outcome"] == golden["data"]["outcome"], entry["name"]
        if isinstance(golden["data"].get("result"), dict):
            assert set(live["data"]["result"]) == set(golden["data"]["result"]), entry["name"]
            assert (
                live["data"]["result"]["result_kind"] == golden["data"]["result"]["result_kind"]
            ), entry["name"]
        if "result_kind" in golden["data"]:
            assert live["data"]["result_kind"] == golden["data"]["result_kind"], entry["name"]
        return
    assert live["error"] is not None, entry["name"]
    assert live["error"]["code"] == golden["error"]["code"], entry["name"]
    assert live["error"]["message"] == golden["error"]["message"], entry["name"]
    assert live["error"]["retryable"] == golden["error"]["retryable"], entry["name"]
    assert set(live["error"]["details"]) == set(golden["error"]["details"]), entry["name"]
    assert response.headers["cache-control"] == "no-store", entry["name"]


def test_every_route_reachable_entry_replays_against_the_real_routes(
    offline_harness: SmallFileWireHarness,
    policy_harness: SmallFileWireHarness,
) -> None:
    drivers = _offline_drivers(offline_harness) | _policy_drivers(policy_harness)
    entries = _entries()
    reachable = [entry for entry in entries if entry["name"] not in _ROUTE_UNREACHABLE_ENTRIES]
    assert {entry["name"] for entry in reachable} == set(drivers), (
        "the wire corpus and the route replay drivers must list the same route-reachable entries"
    )
    for entry in reachable:
        response = drivers[str(entry["name"])]()
        _assert_envelope_matches_entry(entry, response)


def test_route_unreachable_entries_stay_corpus_only_plugin_coverage() -> None:
    """The route-unreachable shapes cannot be produced through these routes.

    A committed identity always answers its frozen replay first, so no
    request sequence reaches ``small_file_upload_state_invalid``; and the
    typed create-time ``source_locator_conflict`` answers only from the
    durable publication store's guarded locator pre-check, which the offline
    harness double does not model. The three device-sync entries ride the
    reconciliation child's device-sync route family, not this harness's two
    small-file routes. Every entry exists so its plugin-side landing is
    still pinned in the TypeScript replay.
    """

    names = {entry["name"] for entry in _entries()}
    assert names >= _ROUTE_UNREACHABLE_ENTRIES


def test_the_device_sync_entries_pin_their_exact_closed_landings() -> None:
    """The device-sync corpus entries are the plugin's cross-language pin.

    Each entry carries the canonical error envelope of one registered
    device-sync code with its registered status, the registry's exact safe
    message and retryability, a UUID-shaped request id, and names the closed
    plugin reason the TypeScript replay must land through the hand-mirrored
    device client — the cursor gap of a pull, the policy advance of a
    finalize and the pre-stream download integrity rejection.
    """

    expected: Final[dict[str, str]] = {
        "device_error_cursor_gap": "device_cursor_gap",
        "device_error_manifest_policy_advanced": "device_manifest_policy_advanced",
        "device_error_download_integrity_failed": "device_download_integrity_failed",
    }
    by_name = {str(entry["name"]): entry for entry in _entries()}
    assert expected.keys() <= by_name.keys()
    for name, code in expected.items():
        entry = by_name[name]
        golden = json.loads(str(entry["body_text"]))
        definition = ERROR_DEFINITIONS[ErrorCode(code)]
        assert entry["surface"] in _DEVICE_SYNC_SURFACES, name
        assert entry["status"] == HTTP_ERROR_STATUSES[ErrorCode(code)], name
        assert golden["data"] is None, name
        assert UUID(str(golden["request_id"])), name
        assert golden["error"] == {
            "code": code,
            "message": definition.safe_message,
            "retryable": definition.is_retryable,
            "details": {},
        }, name
        assert entry["plugin_expectation"] == {"kind": "device_sync_failure", "reason": code}, name


# --- replay semantics -------------------------------------------------------------------


def test_a_frozen_terminal_result_replays_identically_through_both_surfaces(
    offline_harness: SmallFileWireHarness,
) -> None:
    """Replay semantics (spec 10.3): the receipt is frozen, not recomputed.

    The same identity re-preflight and the same-token re-upload both answer
    the exact original terminal result — byte-equal members, no second
    reservation and no second publication.
    """

    harness = offline_harness
    body = _create_body()
    token = _single_part_upload(harness, body)
    committed = _envelope_of(harness.upload(token, _CONTENT))

    replayed_upload = _envelope_of(harness.upload(token, _CONTENT))
    assert replayed_upload["data"] == committed["data"]

    replayed_preflight = _envelope_of(harness.preflight(body))
    assert replayed_preflight["data"]["outcome"] == "committed_replay"
    assert replayed_preflight["data"]["result"] == committed["data"]

    assert harness.sync_state.reservation_count == 1
    assert harness.sync_state.publication_commits == 1


def test_a_frozen_no_change_result_replays_identically(
    offline_harness: SmallFileWireHarness,
) -> None:
    harness = offline_harness
    body = _update_body(source_id=str(uuid4()), base_version_id=str(uuid4()))
    _seeded_current_base(harness, body)

    first = _envelope_of(harness.preflight(body))
    assert first["data"]["outcome"] == "no_change"
    second = _envelope_of(harness.preflight(body))
    assert second["data"] == first["data"]
    assert harness.sync_state.publication_commits == 0
