"""Device sync routes over the offline composition (spec 7.1-7.4).

These tests drive the eight closed routes — event pull, cursor
acknowledgement, the manifest run lifecycle and the verified binary
download — through the real application factory wired with the offline
deterministic web-authentication and device-sync compositions: no database,
no key file, no provider client and no environment read. Every route
accepts exactly the ``obsidian_sync`` access Bearer credential and derives
workspace, device and user from it — no request field ever selects one.
JSON responses carry the canonical envelope and ``Cache-Control: no-store``;
the binary success carries the exact ``Content-Length``, ``Content-Type``,
``X-Content-SHA256`` and correlation headers, pre-stream failures stay
canonical JSON envelopes, and a mid-stream failure terminates the transport
without ever attempting a second JSON body.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import (
    OFFLINE_WEB_ALLOWED_ORIGIN,
    OfflineAuthenticationClock,
    OfflineAuthenticationState,
    compose_offline_web_authentication,
)
from api_runtime.authentication_dependencies import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from api_runtime.device_sync_composition import (
    OfflineDeviceSyncState,
    compose_offline_device_sync,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_os.device_sync.contracts import (
    MAX_MANIFEST_PAGE_ENTRIES,
    DeviceCursorReceipt,
    DeviceEventPage,
    DeviceEventType,
    DeviceSyncEvent,
    ManifestAction,
    ManifestActionKind,
    ManifestActionPage,
    ManifestRunState,
    SourceFingerprint,
)
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.source_locators.values import NormalizedLocator

ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN
_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}
_SHA256: Final[str] = sha256(b"offline device sync content").hexdigest()
_PAGE_DIGEST: Final[str] = sha256(b"canonical page one").hexdigest()
_FINAL_DIGEST: Final[str] = sha256(b"canonical final digest").hexdigest()
_COMMITTED_AT: Final[datetime] = datetime(2026, 8, 26, 9, 30, 0, tzinfo=UTC)
_EXPIRES_AT: Final[datetime] = datetime(2026, 8, 26, 11, 0, 0, tzinfo=UTC)
_OFFLINE_CONTENT: Final[bytes] = b"offline device sync content"

#: Substrings no device sync response may ever contain.
FORBIDDEN_FIELD_MARKERS: Final[tuple[str, ...]] = (
    "receipt",
    "object_key",
    "presign",
    "callback",
    "bucket",
)


class _ReadyProbe:
    """Readiness probe stub: the device sync routes never consult it."""

    async def check(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DeviceSyncRouteHarness:
    """One test client bound to the shared offline state and one device."""

    client: TestClient
    state: OfflineDeviceSyncState
    access_credential: str
    device_id: UUID


def _exchange_device_credential(client: TestClient) -> tuple[str, UUID]:
    """Exchange one approved device grant through the real routes.

    The exchange deliberately exposes only the device identity; the
    workspace and user identities stay credential-private, so scope
    derivation is observed through the device contexts the offline doubles
    capture.
    """

    created = client.post(
        "/api/auth/device-authorizations",
        headers={"Origin": ORIGIN},
        json={
            "client_instance_id": str(uuid4()),
            "device_name": "Personal desktop",
            "platform_class": "obsidian_desktop",
            "platform_name": "windows",
            "plugin_version": "1.4.0",
            "requested_scope": "obsidian_sync",
        },
    )
    assert created.status_code == 200, created.text
    grant = dict(created.json()["data"])
    login = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    assert login.status_code == 200, login.text
    cookies = login.cookies
    approved = client.post(
        f"/api/auth/device-authorizations/{grant['grant_id']}/approve",
        headers={
            "Origin": ORIGIN,
            "Cookie": (
                f"{SESSION_COOKIE_NAME}={cookies[SESSION_COOKIE_NAME]}; "
                f"{CSRF_COOKIE_NAME}={cookies[CSRF_COOKIE_NAME]}"
            ),
            "X-CSRF-Token": cookies[CSRF_COOKIE_NAME],
        },
    )
    assert approved.status_code == 200, approved.text
    exchanged = client.post(
        f"/api/auth/device-authorizations/{grant['grant_id']}/poll",
        headers={"Authorization": f"Bearer {grant['polling_secret']}"},
    )
    assert exchanged.status_code == 200, exchanged.text
    data = dict(exchanged.json()["data"])
    return str(data["access_credential"]), UUID(str(data["device_id"]))


@pytest.fixture
def harness() -> Any:
    clock = OfflineAuthenticationClock()
    auth_state = OfflineAuthenticationState(totp_active=False)
    sync_state = OfflineDeviceSyncState()
    application: FastAPI = create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(clock=clock, state=auth_state),
        device_sync=compose_offline_device_sync(state=sync_state),
    )
    with TestClient(application, base_url=ORIGIN) as test_client:
        credential, device_id = _exchange_device_credential(test_client)
        yield DeviceSyncRouteHarness(
            client=test_client,
            state=sync_state,
            access_credential=credential,
            device_id=device_id,
        )


def bearer(harness: DeviceSyncRouteHarness) -> dict[str, str]:
    return {"Authorization": f"Bearer {harness.access_credential}"}


def entry_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "local_entry_id": "note-1",
        "known_source_id": None,
        "known_version_id": None,
        "normalized_locator": "notes/note.md",
        "fingerprint": {"sha256": _SHA256, "size_bytes": 26, "media_type": "text/markdown"},
        "observation_generation": 4,
    }
    body.update(overrides)
    return body


# --- authentication gates ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/sync/events"),
        ("post", "/api/sync/cursor-acknowledgements"),
        ("post", "/api/sync/manifests"),
        ("get", f"/api/sync/manifests/{uuid4()}/actions"),
        ("get", f"/api/sources/{uuid4()}/versions/{uuid4()}/content"),
    ],
)
def test_every_route_demands_the_device_access_credential(
    harness: DeviceSyncRouteHarness, method: str, path: str
) -> None:
    request = getattr(harness.client, method)
    kwargs: dict[str, Any] = (
        {"json": {"client_observation_generation": 0}} if method == "post" else {}
    )

    missing = request(path, **kwargs)
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "device_credential_invalid"
    assert missing.headers["cache-control"] == "no-store"

    login = harness.client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    session_only = request(
        path,
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={login.cookies[SESSION_COOKIE_NAME]}"},
        **kwargs,
    )
    assert session_only.status_code == 401
    assert session_only.json()["error"]["code"] == "device_credential_invalid"

    unknown = request(
        path,
        headers={"Authorization": f"Bearer at1.{uuid4()}.{bytes(range(32)).hex()}"},
        **kwargs,
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"]["code"] == "device_credential_invalid"


def test_pull_derives_device_scope_from_the_credential(harness: DeviceSyncRouteHarness) -> None:
    response = harness.client.get("/api/sync/events", headers=bearer(harness))
    assert response.status_code == 200
    assert len(response.json()["data"]["events"]) <= 200
    assert response.headers["cache-control"] == "no-store"

    (context,) = harness.state.pull_contexts
    assert context.device_id == harness.device_id
    assert context.workspace_id != UUID(int=0)
    assert context.user_id != UUID(int=0)


def test_download_derives_device_scope_from_the_credential(harness: DeviceSyncRouteHarness) -> None:
    source_id, version_id = uuid4(), uuid4()
    started = harness.client.post(
        "/api/sync/manifests", headers=bearer(harness), json={"client_observation_generation": 0}
    )
    assert started.status_code == 200, started.text
    response = harness.client.get(
        f"/api/sources/{source_id}/versions/{version_id}/content", headers=bearer(harness)
    )
    assert response.status_code == 200
    (context,) = harness.state.descriptor_contexts
    assert context.device_id == harness.device_id
    # The workspace and user identities are credential-derived and stable
    # across every route of the same bearer, never a request input.
    (start_context, _generation) = harness.state.start_calls[0]
    assert context.workspace_id == start_context.workspace_id
    assert context.user_id == start_context.user_id


# --- pull events ------------------------------------------------------------------------------


def _domain_event(sequence: int) -> DeviceSyncEvent:
    return DeviceSyncEvent(
        event_id=uuid4(),
        event_sequence=sequence,
        event_type=DeviceEventType.RENAMED,
        source_id=uuid4(),
        origin_device_id=None,
        base_version_id=uuid4(),
        current_version_id=uuid4(),
        base_fingerprint=SourceFingerprint(
            sha256=_SHA256, size_bytes=10, media_type="text/markdown"
        ),
        current_fingerprint=None,
        prior_locator=NormalizedLocator("notes/old.md"),
        resulting_locator=NormalizedLocator("notes/new.md"),
        tombstone_id=None,
        committed_at=_COMMITTED_AT,
    )


def test_pull_returns_the_seeded_page_with_its_event_shape(harness: DeviceSyncRouteHarness) -> None:
    harness.state.pull_page = DeviceEventPage(
        acknowledged_sequence=5,
        page_checkpoint_sequence=8,
        delivered_through_sequence=7,
        events=(_domain_event(6), _domain_event(7)),
        has_more=True,
    )
    response = harness.client.get("/api/sync/events", headers=bearer(harness))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert set(payload) == {"request_id", "data", "warnings", "error"}
    data = payload["data"]
    assert data["acknowledged_sequence"] == 5
    assert data["page_checkpoint_sequence"] == 8
    assert data["delivered_through_sequence"] == 7
    assert data["has_more"] is True
    assert [event["event_sequence"] for event in data["events"]] == [6, 7]
    event = data["events"][0]
    assert event["event_type"] == "renamed"
    assert event["prior_locator"] == "notes/old.md"
    assert event["resulting_locator"] == "notes/new.md"
    assert event["base_fingerprint"]["sha256"] == _SHA256


# --- cursor acknowledgement ---------------------------------------------------------------------


def test_acknowledge_advances_the_cursor(harness: DeviceSyncRouteHarness) -> None:
    harness.state.acknowledge_receipt = DeviceCursorReceipt(5, 11)
    response = harness.client.post(
        "/api/sync/cursor-acknowledgements",
        headers=bearer(harness),
        json={"expected_previous_sequence": 5, "applied_through_sequence": 11},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"] == {"acknowledged_sequence": 5, "delivered_through_sequence": 11}
    assert harness.state.acknowledge_calls == [(5, 11)]


@pytest.mark.parametrize(
    ("error_code", "status"),
    [
        (DeviceSyncErrorCode.CURSOR_GAP, 409),
        (DeviceSyncErrorCode.CURSOR_REGRESSION, 409),
        (DeviceSyncErrorCode.CURSOR_ACK_AHEAD, 409),
        (DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE, 503),
    ],
)
def test_acknowledge_maps_typed_errors_to_their_registered_envelopes(
    harness: DeviceSyncRouteHarness, error_code: DeviceSyncErrorCode, status: int
) -> None:
    harness.state.acknowledge_error = DeviceSyncError(error_code)
    response = harness.client.post(
        "/api/sync/cursor-acknowledgements",
        headers=bearer(harness),
        json={"expected_previous_sequence": 0, "applied_through_sequence": 3},
    )
    assert response.status_code == status
    assert response.json()["error"]["code"] == error_code.value
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "body",
    [
        {"expected_previous_sequence": -1, "applied_through_sequence": 3},
        {"expected_previous_sequence": 3, "applied_through_sequence": -1},
        {"expected_previous_sequence": 3},
        {
            "expected_previous_sequence": 3,
            "applied_through_sequence": 3,
            "workspace_id": str(uuid4()),
        },
    ],
)
def test_acknowledge_rejects_non_contract_bodies(
    harness: DeviceSyncRouteHarness, body: dict[str, Any]
) -> None:
    response = harness.client.post(
        "/api/sync/cursor-acknowledgements", headers=bearer(harness), json=body
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"


# --- manifest run lifecycle ---------------------------------------------------------------------


def test_start_manifest_returns_the_run_receipt(harness: DeviceSyncRouteHarness) -> None:
    response = harness.client.post(
        "/api/sync/manifests",
        headers=bearer(harness),
        json={"client_observation_generation": 4},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert set(data) == {
        "manifest_run_id",
        "state",
        "base_acknowledged_sequence",
        "checkpoint_sequence",
        "policy_revision_number",
        "client_observation_generation",
        "next_page_number",
        "entry_count",
        "expires_at",
    }
    assert data["client_observation_generation"] == 4
    assert data["state"] == ManifestRunState.COLLECTING.value
    (context, generation) = harness.state.start_calls[0]
    assert context.device_id == harness.device_id
    assert generation == 4


def test_start_manifest_rejects_identity_selectors_and_negative_generations(
    harness: DeviceSyncRouteHarness,
) -> None:
    for body in (
        {"client_observation_generation": -1},
        {"client_observation_generation": 1, "device_id": str(uuid4())},
    ):
        response = harness.client.post("/api/sync/manifests", headers=bearer(harness), json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "api_request_validation_failed"


def test_append_manifest_page_accepts_the_exact_next_page(harness: DeviceSyncRouteHarness) -> None:
    run_id = uuid4()
    response = harness.client.put(
        f"/api/sync/manifests/{run_id}/pages/0",
        headers=bearer(harness),
        json={"entries": [entry_body()], "page_digest": _PAGE_DIGEST},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert set(data) == {
        "manifest_run_id",
        "page_number",
        "accepted_entry_count",
        "next_page_number",
    }
    assert data["page_number"] == 0
    assert data["accepted_entry_count"] == 1
    assert data["next_page_number"] == 1
    (context, command) = harness.state.append_calls[0]
    assert context.device_id == harness.device_id
    assert command.manifest_run_id == run_id
    assert command.page_number == 0
    assert command.page_digest.hexadecimal == _PAGE_DIGEST
    (converted,) = command.entries
    assert converted.local_entry_id == "note-1"


@pytest.mark.parametrize(
    ("error_code", "status"),
    [
        (DeviceSyncErrorCode.MANIFEST_PAGE_REPLAY_MISMATCH, 409),
        (DeviceSyncErrorCode.MANIFEST_PAGE_INVALID, 422),
        (DeviceSyncErrorCode.MANIFEST_STATE_INVALID, 409),
        (DeviceSyncErrorCode.MANIFEST_NOT_FOUND, 404),
    ],
)
def test_append_manifest_page_maps_typed_errors(
    harness: DeviceSyncRouteHarness, error_code: DeviceSyncErrorCode, status: int
) -> None:
    harness.state.append_error = DeviceSyncError(error_code)
    response = harness.client.put(
        f"/api/sync/manifests/{uuid4()}/pages/0",
        headers=bearer(harness),
        json={"entries": [entry_body()], "page_digest": _PAGE_DIGEST},
    )
    assert response.status_code == status
    assert response.json()["error"]["code"] == error_code.value


@pytest.mark.parametrize(
    "body",
    [
        {"entries": [entry_body()]},
        {"entries": [dict(entry_body(), workspace_id=str(uuid4()))], "page_digest": _PAGE_DIGEST},
        {"entries": [], "page_digest": "not-a-digest"},
        {"page_digest": _PAGE_DIGEST},
        {
            "entries": [
                dict(
                    entry_body(),
                    fingerprint={
                        "sha256": _SHA256,
                        "size_bytes": -1,
                        "media_type": "text/markdown",
                    },
                )
            ],
            "page_digest": _PAGE_DIGEST,
        },
    ],
)
def test_append_manifest_page_rejects_non_contract_bodies(
    harness: DeviceSyncRouteHarness, body: dict[str, Any]
) -> None:
    response = harness.client.put(
        f"/api/sync/manifests/{uuid4()}/pages/0", headers=bearer(harness), json=body
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"


def test_append_manifest_page_rejects_wire_locators_with_bad_grammar(
    harness: DeviceSyncRouteHarness,
) -> None:
    response = harness.client.put(
        f"/api/sync/manifests/{uuid4()}/pages/0",
        headers=bearer(harness),
        json={
            "entries": [entry_body(normalized_locator="notes\\note.md")],
            "page_digest": _PAGE_DIGEST,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == DeviceSyncErrorCode.MANIFEST_PAGE_INVALID.value


def test_finalize_manifest_returns_the_planned_run(harness: DeviceSyncRouteHarness) -> None:
    run_id = uuid4()
    response = harness.client.post(
        f"/api/sync/manifests/{run_id}/finalize",
        headers=bearer(harness),
        json={"total_entry_count": 3, "final_digest": _FINAL_DIGEST},
    )
    assert response.status_code == 200
    assert response.json()["data"]["state"] == ManifestRunState.PLANNED.value
    (_context, command) = harness.state.finalize_calls[0]
    assert command.manifest_run_id == run_id
    assert command.total_entry_count == 3
    assert command.final_digest.hexadecimal == _FINAL_DIGEST


def test_finalize_maps_digest_mismatch_to_its_registered_envelope(
    harness: DeviceSyncRouteHarness,
) -> None:
    harness.state.finalize_error = DeviceSyncError(DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH)
    response = harness.client.post(
        f"/api/sync/manifests/{uuid4()}/finalize",
        headers=bearer(harness),
        json={"total_entry_count": 3, "final_digest": _FINAL_DIGEST},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "device_manifest_digest_mismatch"


def test_list_manifest_actions_returns_the_seeded_page_and_bounds_the_query(
    harness: DeviceSyncRouteHarness,
) -> None:
    run_id = uuid4()
    harness.state.actions_page = ManifestActionPage(
        manifest_run_id=run_id,
        actions=(
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
        ),
        has_more=False,
    )
    response = harness.client.get(
        f"/api/sync/manifests/{run_id}/actions",
        headers=bearer(harness),
        params={"after_action_index": 0, "limit": MAX_MANIFEST_PAGE_ENTRIES},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["manifest_run_id"] == str(run_id)
    assert data["has_more"] is False
    (action,) = data["actions"]
    assert set(action) == {
        "action_index",
        "action_kind",
        "local_entry_id",
        "source_id",
        "source_version_id",
        "source_locator_id",
        "source_tombstone_id",
        "reason",
        "checkpoint_locator",
    }
    assert action["action_kind"] == "upload"
    # The checkpoint locator renders closed on every non-download action.
    assert action["checkpoint_locator"] is None
    assert harness.state.actions_calls[0].after_action_index == 0
    assert harness.state.actions_calls[0].limit == MAX_MANIFEST_PAGE_ENTRIES


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": MAX_MANIFEST_PAGE_ENTRIES + 1},
        {"after_action_index": -1},
    ],
)
def test_list_manifest_actions_rejects_out_of_bound_queries(
    harness: DeviceSyncRouteHarness, params: dict[str, int]
) -> None:
    response = harness.client.get(
        f"/api/sync/manifests/{uuid4()}/actions", headers=bearer(harness), params=params
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"


def test_complete_manifest_advances_the_cursor(harness: DeviceSyncRouteHarness) -> None:
    harness.state.complete_receipt = DeviceCursorReceipt(9, 12)
    response = harness.client.post(
        f"/api/sync/manifests/{uuid4()}/complete",
        headers=bearer(harness),
        json={"final_digest": _FINAL_DIGEST},
    )
    assert response.status_code == 200
    assert response.json()["data"] == {"acknowledged_sequence": 9, "delivered_through_sequence": 12}


@pytest.mark.parametrize(
    ("error_code", "status"),
    [
        (DeviceSyncErrorCode.MANIFEST_EXPIRED, 410),
        (DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED, 409),
        (DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE, 503),
    ],
)
def test_complete_manifest_maps_typed_errors(
    harness: DeviceSyncRouteHarness, error_code: DeviceSyncErrorCode, status: int
) -> None:
    harness.state.complete_error = DeviceSyncError(error_code)
    response = harness.client.post(
        f"/api/sync/manifests/{uuid4()}/complete",
        headers=bearer(harness),
        json={"final_digest": _FINAL_DIGEST},
    )
    assert response.status_code == status
    assert response.json()["error"]["code"] == error_code.value


# --- binary download -----------------------------------------------------------------------------


def test_download_streams_verified_bytes_with_exact_headers(
    harness: DeviceSyncRouteHarness,
) -> None:
    source_id, version_id = uuid4(), uuid4()
    harness.state.content_bytes = _OFFLINE_CONTENT
    response = harness.client.get(
        f"/api/sources/{source_id}/versions/{version_id}/content", headers=bearer(harness)
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-length"] == str(len(_OFFLINE_CONTENT))
    assert response.headers["x-content-sha256"] == _SHA256
    assert response.headers["x-request-id"]
    assert response.content == _OFFLINE_CONTENT
    assert response.headers["cache-control"] == "no-store"
    assert harness.state.reader_closed is True


def test_download_pre_stream_failures_remain_json_envelopes(
    harness: DeviceSyncRouteHarness,
) -> None:
    harness.state.content_error = DeviceSyncError(DeviceSyncErrorCode.EVENT_UNAVAILABLE)
    missing = harness.client.get(
        f"/api/sources/{uuid4()}/versions/{uuid4()}/content", headers=bearer(harness)
    )
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/json")
    assert missing.json()["error"]["code"] == "device_event_unavailable"

    harness.state.content_error = None
    harness.state.is_policy_denied = True
    denied = harness.client.get(
        f"/api/sources/{uuid4()}/versions/{uuid4()}/content", headers=bearer(harness)
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "exclusion_policy_denied"

    harness.state.is_policy_denied = False
    harness.state.content_error = DeviceSyncError(DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED)
    corrupt = harness.client.get(
        f"/api/sources/{uuid4()}/versions/{uuid4()}/content", headers=bearer(harness)
    )
    assert corrupt.status_code == 422
    assert corrupt.json()["error"]["code"] == "device_download_integrity_failed"


def test_download_dependency_outage_is_the_retryable_envelope(
    harness: DeviceSyncRouteHarness,
) -> None:
    harness.state.content_error = DeviceSyncError(DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE)
    response = harness.client.get(
        f"/api/sources/{uuid4()}/versions/{uuid4()}/content", headers=bearer(harness)
    )
    assert response.status_code == 503
    assert response.json()["error"]["retryable"] is True


async def _raw_asgi_download(
    app: FastAPI, credential: str, source_id: UUID, version_id: UUID
) -> tuple[int, dict[bytes, bytes], list[bytes], BaseException | None]:
    """Drive one download through raw ASGI, capturing every message.

    The receive seam follows the canonical streaming shape: it answers the
    one request-body event and then suspends until disconnect, so the
    response's disconnect listener yields control to the streaming task the
    way a real server's receive would.
    """

    started: dict[str, Any] | None = None
    body_chunks: list[bytes] = []
    caught: BaseException | None = None
    request_body_delivered = False
    disconnected = asyncio.Event()

    async def receive() -> Mapping[str, Any]:
        nonlocal request_body_delivered
        if not request_body_delivered:
            request_body_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Mapping[str, Any]) -> None:
        nonlocal started
        if message["type"] == "http.response.start":
            started = dict(message)
        elif message["type"] == "http.response.body":
            body_chunks.append(message["body"])

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/api/sources/{source_id}/versions/{version_id}/content",
        "raw_path": f"/api/sources/{source_id}/versions/{version_id}/content".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"authorization", f"Bearer {credential}".encode())],
        "client": ("127.0.0.1", 42000),
        "server": ("127.0.0.1", 80),
    }
    try:
        await app(scope, receive, send)
    except BaseException as error:  # the server must still observe the failure
        caught = error
    finally:
        disconnected.set()
    assert started is not None
    headers = {name.lower(): value for name, value in started["headers"]}
    return int(started["status"]), headers, body_chunks, caught


@pytest.mark.asyncio
async def test_download_mid_stream_failure_terminates_transport_without_json(
    harness: DeviceSyncRouteHarness,
) -> None:
    harness.state.content_bytes = _OFFLINE_CONTENT
    harness.state.mid_stream_error = RuntimeError("sensitive-mid-stream-failure")
    status, headers, chunks, caught = await _raw_asgi_download(
        harness.client.app,
        harness.access_credential,
        uuid4(),
        uuid4(),
    )
    assert status == 200
    assert caught is not None
    # Only the verified pre-failure bytes shipped; no JSON envelope rewrite.
    assert chunks == [_OFFLINE_CONTENT]
    assert b'"error"' not in b"".join(chunks)
    assert headers[b"x-content-sha256"].decode() == _SHA256
    assert harness.state.reader_closed is True


# --- response hygiene -----------------------------------------------------------------------------


def test_no_response_ever_carries_receipt_object_or_provider_fields(
    harness: DeviceSyncRouteHarness,
) -> None:
    harness.state.acknowledge_error = DeviceSyncError(DeviceSyncErrorCode.CURSOR_GAP)
    responses = (
        harness.client.get("/api/sync/events", headers=bearer(harness)),
        harness.client.post(
            "/api/sync/cursor-acknowledgements",
            headers=bearer(harness),
            json={"expected_previous_sequence": 0, "applied_through_sequence": 0},
        ),
        harness.client.post(
            "/api/sync/manifests",
            headers=bearer(harness),
            json={"client_observation_generation": 0},
        ),
        harness.client.put(
            f"/api/sync/manifests/{uuid4()}/pages/0",
            headers=bearer(harness),
            json={"entries": [entry_body()], "page_digest": _PAGE_DIGEST},
        ),
        harness.client.get(f"/api/sync/manifests/{uuid4()}/actions", headers=bearer(harness)),
        harness.client.get(
            f"/api/sources/{uuid4()}/versions/{uuid4()}/content", headers=bearer(harness)
        ),
    )
    for response in responses:
        rendered = response.text
        for marker in FORBIDDEN_FIELD_MARKERS:
            assert marker not in rendered


def test_every_json_response_carries_the_bound_request_id(
    harness: DeviceSyncRouteHarness,
) -> None:
    response = harness.client.get("/api/sync/events", headers=bearer(harness))
    assert response.json()["request_id"] == response.headers["x-request-id"]


# --- the offline state defaults ------------------------------------------------------------------


def test_offline_state_defaults_are_zero_cursor_and_empty_pages(
    harness: DeviceSyncRouteHarness,
) -> None:
    pull = harness.client.get("/api/sync/events", headers=bearer(harness))
    data = pull.json()["data"]
    assert data["acknowledged_sequence"] == 0
    assert data["events"] == []
    assert data["has_more"] is False

    actions = harness.client.get(
        f"/api/sync/manifests/{uuid4()}/actions", headers=bearer(harness)
    )
    assert actions.json()["data"]["actions"] == []
    assert actions.json()["data"]["has_more"] is False

    complete = harness.client.post(
        f"/api/sync/manifests/{uuid4()}/complete",
        headers=bearer(harness),
        json={"final_digest": _FINAL_DIGEST},
    )
    assert complete.json()["data"] == {
        "acknowledged_sequence": 0,
        "delivered_through_sequence": 0,
    }


def test_offline_run_expiry_is_one_hour(harness: DeviceSyncRouteHarness) -> None:
    response = harness.client.post(
        "/api/sync/manifests", headers=bearer(harness), json={"client_observation_generation": 0}
    )
    expires_at = datetime.fromisoformat(response.json()["data"]["expires_at"])
    started_at = datetime.now(UTC)
    assert timedelta(hours=1) - (expires_at - started_at) < timedelta(minutes=1)
    assert harness.state.start_calls[0][0].device_id == harness.device_id
