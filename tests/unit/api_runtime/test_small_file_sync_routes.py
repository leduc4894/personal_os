"""Small-file sync routes over the offline compositions (spec 10.1-10.3).

These tests drive the two closed routes — the journal-event preflight and the
operation-bound content stream — through the real application factory wired
with the offline deterministic web-authentication and small-file-sync
compositions: no database, no key file, no object store and no environment
read. Both routes accept exactly the ``obsidian_sync`` access Bearer
credential and derive workspace and device from it — no request field ever
selects one. Preflight maps each of the five typed outcomes onto the canonical
data envelope with exactly its safe payload members; the content route
enforces the server-owned 16 MiB ceiling before anything can publish, maps a
broken or mismatching stream onto the closed integrity failure, and never
exposes a receipt, object key or provider detail. Every response carries
``Cache-Control: no-store``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
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
from api_runtime.small_file_sync_composition import (
    OfflineSmallFileSyncState,
    compose_offline_small_file_sync,
)
from api_runtime.small_file_sync_routes import (
    CONTENT_READ_DEADLINE_SECONDS,
    bounded_content_stream,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.small_file_sync.contracts import (
    MAX_SINGLE_PART_FILE_SIZE_BYTES,
    MAX_UPLOAD_FILE_SIZE_BYTES,
)
from personal_os.sources.commands import SourceType
from personal_os.sources.reading import CanonicalSourceReference

ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN
_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}
_CONTENT: Final[bytes] = b"# small-file canonical content for the sync routes\n"
_CONTENT_DIGEST: Final[str] = sha256(_CONTENT).hexdigest()
_MEDIA_TYPE: Final[str] = "text/markdown"
_CURRENT_BASE_COMMITTED_AT: Final[datetime] = datetime(2026, 8, 18, 9, 30, 0, tzinfo=UTC)

#: Terminal-receipt hygiene: the exact members a terminal result may carry.
TERMINAL_RESULT_MEMBERS: Final[frozenset[str]] = frozenset(
    {"result_kind", "source_id", "source_version_id", "content_version", "committed_at"}
)
#: Substrings no small-file response may ever contain.
FORBIDDEN_FIELD_MARKERS: Final[tuple[str, ...]] = (
    "receipt",
    "object_key",
    "provider",
    "bucket",
    "etag",
    "presign",
    "callback",
)


class _ReadyProbe:
    """Readiness probe stub: the sync routes never consult dependencies."""

    async def check(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SmallFileRouteHarness:
    """One test client bound to the shared offline state and one device."""

    client: TestClient
    sync_state: OfflineSmallFileSyncState
    access_credential: str
    device_id: UUID


def _exchange_device_credential(client: TestClient) -> tuple[str, UUID]:
    """Exchange one approved device grant through the real routes."""

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
def harness() -> Iterator[SmallFileRouteHarness]:
    clock = OfflineAuthenticationClock()
    auth_state = OfflineAuthenticationState(totp_active=False)
    sync_state = OfflineSmallFileSyncState()
    application: FastAPI = create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(clock=clock, state=auth_state),
        small_file_sync=compose_offline_small_file_sync(state=sync_state),
    )
    with TestClient(application, base_url=ORIGIN) as test_client:
        credential, device_id = _exchange_device_credential(test_client)
        yield SmallFileRouteHarness(
            client=test_client,
            sync_state=sync_state,
            access_credential=credential,
            device_id=device_id,
        )


def bearer(harness: SmallFileRouteHarness) -> dict[str, str]:
    return {"Authorization": f"Bearer {harness.access_credential}"}


def create_body(
    *,
    content: bytes = _CONTENT,
    sha256_text: str = _CONTENT_DIGEST,
    size_bytes: int | None = None,
    media_type: str = _MEDIA_TYPE,
    locator: str = "notes/synced-note.md",
) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "operation": "create",
        "local_file_id": str(uuid4()),
        "source_id": None,
        "base_version_id": None,
        "normalized_locator": locator,
        "sha256": sha256_text,
        "size_bytes": len(content) if size_bytes is None else size_bytes,
        "media_type": media_type,
        "policy_revision": 7,
    }


def preflight(
    harness: SmallFileRouteHarness, body: dict[str, Any], *, credential: str | None = None
) -> Any:
    return harness.client.post(
        "/api/sync/journal-events/preflight",
        headers={"Authorization": f"Bearer {credential or harness.access_credential}"},
        json=body,
    )


def upload(harness: SmallFileRouteHarness, token: str, content: bytes) -> Any:
    return harness.client.put(
        f"/api/uploads/{token}/content",
        headers={**bearer(harness), "Content-Type": "application/octet-stream"},
        content=content,
    )


def single_part_token(harness: SmallFileRouteHarness, body: dict[str, Any]) -> str:
    response = preflight(harness, body)
    assert response.status_code == 200, response.text
    data = dict(response.json()["data"])
    assert data["outcome"] == "single_part_upload", data
    return str(data["operation_id"])


# --- authentication gates ------------------------------------------------------------------


@pytest.mark.parametrize("path_and_method", [("post", "preflight"), ("put", "content")])
def test_both_routes_demand_the_device_access_credential(
    harness: SmallFileRouteHarness, path_and_method: tuple[str, str]
) -> None:
    method, surface = path_and_method
    path = (
        "/api/sync/journal-events/preflight"
        if surface == "preflight"
        else f"/api/uploads/{'a' * 32}/content"
    )
    request = getattr(harness.client, method)
    missing = request(path, json={} if method == "post" else None)
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "device_credential_invalid"
    assert missing.headers["cache-control"] == "no-store"

    login = harness.client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    session_only = request(
        path,
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={login.cookies[SESSION_COOKIE_NAME]}"},
        json={} if method == "post" else None,
    )
    assert session_only.status_code == 401
    assert session_only.json()["error"]["code"] == "device_credential_invalid"

    unknown = request(
        path,
        headers={"Authorization": f"Bearer at1.{uuid4()}.{bytes(range(32)).hex()}"},
        json={} if method == "post" else None,
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"]["code"] == "device_credential_invalid"
    assert unknown.headers["cache-control"] == "no-store"


def test_workspace_and_device_derive_from_the_credential(harness: SmallFileRouteHarness) -> None:
    """A foreign device cannot stream another device's operation token."""

    token = single_part_token(harness, create_body())
    foreign_client = harness.client  # a second credential minted inside one app
    created = foreign_client.post(
        "/api/auth/device-authorizations",
        headers={"Origin": ORIGIN},
        json={
            "client_instance_id": str(uuid4()),
            "device_name": "Other desktop",
            "platform_class": "obsidian_desktop",
            "platform_name": "windows",
            "plugin_version": "1.4.0",
            "requested_scope": "obsidian_sync",
        },
    )
    assert created.status_code == 200, created.text
    grant = dict(created.json()["data"])
    login = foreign_client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    cookies = login.cookies
    approved = foreign_client.post(
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
    exchanged = foreign_client.post(
        f"/api/auth/device-authorizations/{grant['grant_id']}/poll",
        headers={"Authorization": f"Bearer {grant['polling_secret']}"},
    )
    assert exchanged.status_code == 200, exchanged.text
    foreign_credential = str(exchanged.json()["data"]["access_credential"])

    mismatched = harness.client.put(
        f"/api/uploads/{token}/content",
        headers={
            "Authorization": f"Bearer {foreign_credential}",
            "Content-Type": "application/octet-stream",
        },
        content=_CONTENT,
    )
    assert mismatched.status_code == 409
    assert mismatched.json()["error"]["code"] == "small_file_operation_identity_mismatch"
    assert harness.sync_state.publication_commits == 0


# --- preflight input hygiene ----------------------------------------------------------------


def test_preflight_rejects_malformed_json(harness: SmallFileRouteHarness) -> None:
    response = harness.client.post(
        "/api/sync/journal-events/preflight",
        headers={**bearer(harness), "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "api_request_malformed"


@pytest.mark.parametrize(
    "body",
    [
        # no request field may choose the workspace, device or user
        {**create_body(), "workspace_id": str(uuid4())},
        {**create_body(), "device_id": str(uuid4())},
        {**create_body(), "user_id": str(uuid4())},
        {**create_body(), "policy_revision_number": 7},
        # create must carry neither base member
        {**create_body(), "source_id": str(uuid4())},
        {**create_body(), "base_version_id": str(uuid4())},
        # missing required members
        {name: value for name, value in create_body().items() if name != "sha256"},
        {name: value for name, value in create_body().items() if name != "event_id"},
        # closed operation vocabulary
        {**create_body(), "operation": "delete"},
        # the declared digest follows its own closed wire grammar
        {**create_body(), "sha256": "not-a-digest"},
        # update requires both base members
        {
            **create_body(),
            "operation": "update",
            "source_id": str(uuid4()),
            "base_version_id": None,
        },
        {**create_body(), "operation": "update", "source_id": None},
    ],
)
def test_preflight_rejects_non_contract_bodies(
    harness: SmallFileRouteHarness, body: dict[str, Any]
) -> None:
    response = preflight(harness, body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] in {
        "api_request_validation_failed",
        "small_file_preflight_invalid",
    }
    assert harness.sync_state.reservation_count == 0


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        # the wire grammar passes; the frozen domain grammar rejects
        (
            {**create_body(), "idempotency_key": "00000000-0000-0000-0000-000000000000"},
            "idempotency_key_invalid",
        ),
        ({**create_body(), "media_type": "text/markdown; charset=utf-8"}, "media_type_invalid"),
        ({**create_body(), "normalized_locator": "notes\\synced-note.md"}, "locator_invalid"),
        ({**create_body(), "policy_revision": 0}, "policy_revision_invalid"),
    ],
)
def test_preflight_maps_domain_grammar_violations_to_closed_reasons(
    harness: SmallFileRouteHarness, body: dict[str, Any], reason: str
) -> None:
    response = preflight(harness, body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "small_file_preflight_invalid"
    assert error["details"]["reason"] == reason


def test_preflight_rejects_declared_size_over_the_server_ceiling(
    harness: SmallFileRouteHarness,
) -> None:
    body = create_body(size_bytes=MAX_UPLOAD_FILE_SIZE_BYTES + 1)
    response = preflight(harness, body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "small_file_size_limit_exceeded"
    assert harness.sync_state.reservation_count == 0


def test_preflight_routes_single_part_overload_to_the_multipart_outcome(
    harness: SmallFileRouteHarness,
) -> None:
    """One byte above the routing constant is a server-owned multipart plan.

    The preflight outcome stays payload-free: the client derives its opaque
    session, geometry and part URLs only from the multipart session
    endpoints after this decision (Child 7 spec 4).
    """

    body = create_body(size_bytes=MAX_SINGLE_PART_FILE_SIZE_BYTES + 1)
    response = preflight(harness, body)
    assert response.status_code == 200
    data = dict(response.json()["data"])
    assert data == {"outcome": "multipart_upload"}
    assert harness.sync_state.reservation_count == 0


# --- preflight outcomes ----------------------------------------------------------------------


def test_create_preflight_returns_the_single_part_upload_operation(
    harness: SmallFileRouteHarness,
) -> None:
    response = preflight(harness, create_body())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = dict(response.json()["data"])
    assert set(data) == {"outcome", "operation_id", "expires_at"}
    assert data["outcome"] == "single_part_upload"
    token = str(data["operation_id"])
    assert 32 <= len(token) <= 128
    assert all(char.isalnum() or char in {"-", "_"} for char in token)
    assert data["expires_at"] is not None
    assert harness.sync_state.reservation_count == 1


def test_denied_policy_returns_the_excluded_outcome(harness: SmallFileRouteHarness) -> None:
    harness.sync_state.is_policy_denied = True
    response = preflight(harness, create_body())
    assert response.status_code == 200
    data = dict(response.json()["data"])
    assert data == {"outcome": "excluded"}
    assert harness.sync_state.reservation_count == 0


def _current_reference(
    body: dict[str, Any], *, source_version_id: UUID | None = None
) -> CanonicalSourceReference:
    source_id = UUID(str(body["source_id"])) if body.get("source_id") else uuid4()
    base_version_id = UUID(str(body["base_version_id"])) if body.get("base_version_id") else uuid4()
    return CanonicalSourceReference(
        workspace_id=uuid4(),
        source_id=source_id,
        source_version_id=source_version_id if source_version_id is not None else base_version_id,
        content_version=3,
        source_type=SourceType.MARKDOWN,
        expected_object=ExpectedObject(
            content_digest=ContentDigest.parse(str(body["sha256"])),
            size_bytes=int(body["size_bytes"]),
            media_type=CanonicalMediaType.parse(str(body["media_type"])),
        ),
        committed_at=_CURRENT_BASE_COMMITTED_AT,
    )


def _update_body(*, digest_matches_current: bool) -> dict[str, Any]:
    different = b"# changed local bytes\n"
    declared = _CONTENT_DIGEST if digest_matches_current else sha256(different).hexdigest()
    size = len(_CONTENT) if digest_matches_current else len(different)
    return {
        **create_body(sha256_text=declared, size_bytes=size),
        "operation": "update",
        "source_id": str(uuid4()),
        "base_version_id": str(uuid4()),
    }


def test_stale_update_base_returns_the_conflict_outcome_with_its_capture_grant(
    harness: SmallFileRouteHarness,
) -> None:
    """The conflict wire verdict now surfaces its capture grant (Child 8).

    A stale single-part-sized update reserves one capture operation, and the
    preflight answer carries exactly the plugin needs to reach the captured
    conflict: the outcome, the opaque operation grant with its expiry, and
    — on a same-identity replay after capture — the replayed conflict
    identity. No terminal result, receipt or content member ever renders.
    """

    body = _update_body(digest_matches_current=False)
    harness.sync_state.current_reference = _current_reference(body, source_version_id=uuid4())
    response = preflight(harness, body)
    assert response.status_code == 200
    data = dict(response.json()["data"])
    assert set(data) == {"outcome", "operation_id", "expires_at"}
    assert data["outcome"] == "conflict"
    token = str(data["operation_id"])
    assert 32 <= len(token) <= 128
    assert all(char.isalnum() or char in {"-", "_"} for char in token)
    assert data["expires_at"] is not None
    assert harness.sync_state.reservation_count == 1
    assert harness.sync_state.publication_commits == 0


def test_missing_current_reference_grants_the_capture_operation(
    harness: SmallFileRouteHarness,
) -> None:
    """A server-deleted source keeps the conflict verdict and its grant."""

    harness.sync_state.current_reference = None
    body = _update_body(digest_matches_current=False)
    harness.sync_state.deleted_source_ids.add(UUID(str(body["source_id"])))
    response = preflight(harness, body)
    assert response.status_code == 200
    data = dict(response.json()["data"])
    assert data["outcome"] == "conflict"
    assert set(data) == {"outcome", "operation_id", "expires_at"}
    assert harness.sync_state.reservation_count == 1
    assert harness.sync_state.publication_commits == 0


_DECLARED_CANDIDATE: Final[bytes] = b"# changed local bytes\n"


def test_conflict_content_route_captures_the_verified_candidate(
    harness: SmallFileRouteHarness,
) -> None:
    """The capture grant is exercisable over the wire (Child 8 spec 5.1).

    A stale-base preflight grant uploads its candidate through the dedicated
    conflict-content route; the answer is exactly the opaque capture
    receipt, a same-token replay returns the frozen conflict, and no
    publication ever runs.
    """

    body = _update_body(digest_matches_current=False)
    harness.sync_state.current_reference = _current_reference(body, source_version_id=uuid4())
    granted = dict(preflight(harness, body).json()["data"])
    token = str(granted["operation_id"])

    response = harness.client.put(
        f"/api/uploads/{token}/conflict-content",
        headers={**bearer(harness), "Content-Type": "application/octet-stream"},
        content=_DECLARED_CANDIDATE,
    )

    assert response.status_code == 200, response.text
    data = dict(response.json()["data"])
    assert set(data) == {
        "conflict_id",
        "source_id",
        "observed_remote_version_id",
        "captured_at",
    }
    assert data["source_id"] == body["source_id"]
    assert response.headers["cache-control"] == "no-store"
    assert harness.sync_state.conflict_capture_count == 1
    assert harness.sync_state.publication_commits == 0

    replayed = harness.client.put(
        f"/api/uploads/{token}/conflict-content",
        headers={**bearer(harness), "Content-Type": "application/octet-stream"},
        content=_DECLARED_CANDIDATE,
    )
    assert replayed.status_code == 200, replayed.text
    assert dict(replayed.json()["data"]) == data
    assert harness.sync_state.conflict_capture_count == 1


def test_conflict_content_route_demands_the_device_access_credential(
    harness: SmallFileRouteHarness,
) -> None:
    response = harness.client.put(
        f"/api/uploads/{'a' * 40}/conflict-content",
        headers={"Content-Type": "application/octet-stream"},
        content=_CONTENT,
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "device_credential_invalid"


def test_conflict_content_route_rejects_a_publication_operation_token(
    harness: SmallFileRouteHarness,
) -> None:
    """A publication grant can never double as a capture grant."""

    token = single_part_token(harness, create_body())

    response = harness.client.put(
        f"/api/uploads/{token}/conflict-content",
        headers={**bearer(harness), "Content-Type": "application/octet-stream"},
        content=_CONTENT,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "small_file_upload_state_invalid"
    assert harness.sync_state.publication_commits == 0
    assert harness.sync_state.conflict_capture_count == 0


def test_conflict_content_route_rejects_digest_mismatch_without_capturing(
    harness: SmallFileRouteHarness,
) -> None:
    body = _update_body(digest_matches_current=False)
    harness.sync_state.current_reference = _current_reference(body, source_version_id=uuid4())
    granted = dict(preflight(harness, body).json()["data"])
    token = str(granted["operation_id"])

    response = harness.client.put(
        f"/api/uploads/{token}/conflict-content",
        headers={**bearer(harness), "Content-Type": "application/octet-stream"},
        content=b"different bytes that fail the declared digest",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "small_file_content_integrity_failed"
    assert harness.sync_state.conflict_capture_count == 0


def test_matching_update_base_returns_the_no_change_outcome(
    harness: SmallFileRouteHarness,
) -> None:
    body = _update_body(digest_matches_current=True)
    harness.sync_state.current_reference = _current_reference(body)
    response = preflight(harness, body)
    assert response.status_code == 200
    data = dict(response.json()["data"])
    assert data["outcome"] == "no_change"
    assert set(data) == {"outcome", "result"}
    assert set(data["result"]) == TERMINAL_RESULT_MEMBERS
    assert data["result"]["result_kind"] == "no_change"


def test_same_identity_preflight_after_commit_replays_exactly(
    harness: SmallFileRouteHarness,
) -> None:
    body = create_body()
    token = single_part_token(harness, body)
    committed = upload(harness, token, _CONTENT)
    assert committed.status_code == 200, committed.text

    replay = preflight(harness, body)
    assert replay.status_code == 200
    data = dict(replay.json()["data"])
    assert data["outcome"] == "committed_replay"
    assert set(data) == {"outcome", "result"}
    assert data["result"] == committed.json()["data"]
    # the replay allocates neither another operation nor another publication
    assert harness.sync_state.reservation_count == 1
    assert harness.sync_state.publication_commits == 1


# --- content stream --------------------------------------------------------------------------


def test_content_stream_commits_the_terminal_result(harness: SmallFileRouteHarness) -> None:
    token = single_part_token(harness, create_body())
    response = upload(harness, token, _CONTENT)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = dict(response.json()["data"])
    assert set(data) == TERMINAL_RESULT_MEMBERS
    assert data["result_kind"] == "committed"
    assert data["content_version"] == 1
    assert harness.sync_state.publication_commits == 1

    lost_response_replay = upload(harness, token, _CONTENT)
    assert lost_response_replay.status_code == 200
    assert lost_response_replay.json()["data"] == data
    assert harness.sync_state.publication_commits == 1


def test_content_stream_rejects_digest_mismatch_without_publishing(
    harness: SmallFileRouteHarness,
) -> None:
    token = single_part_token(harness, create_body())
    response = upload(harness, token, b"# these are different bytes entirely\n")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "small_file_content_integrity_failed"
    assert harness.sync_state.publication_commits == 0
    assert harness.sync_state.stored_digest_count == 0


def test_content_stream_rejects_bodies_over_the_server_ceiling(
    harness: SmallFileRouteHarness,
) -> None:
    oversized = create_body(content=b"", size_bytes=MAX_SINGLE_PART_FILE_SIZE_BYTES)
    oversized["sha256"] = "0" * 64
    token = single_part_token(harness, oversized)
    over_limit = b"x" * (MAX_SINGLE_PART_FILE_SIZE_BYTES + 1)
    response = upload(harness, token, over_limit)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "small_file_size_limit_exceeded"
    assert harness.sync_state.stored_digest_count == 0
    assert harness.sync_state.publication_commits == 0


@pytest.mark.parametrize(
    "token",
    [
        "short-token",
        "!!!invalid-tokens-are-not-urlsafe!!!",
        "00000000-0000-7000-8000-0000000000ab",
    ],
)
def test_content_stream_rejects_malformed_operation_tokens(
    harness: SmallFileRouteHarness, token: str
) -> None:
    response = harness.client.put(
        f"/api/uploads/{token}/content",
        headers={**bearer(harness), "Content-Type": "application/octet-stream"},
        content=_CONTENT,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"


def test_content_stream_rejects_unknown_operation_tokens(
    harness: SmallFileRouteHarness,
) -> None:
    token = "a" * 43
    response = upload(harness, token, _CONTENT)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "small_file_operation_not_found"


def test_content_stream_rejects_expired_operations(harness: SmallFileRouteHarness) -> None:
    harness.sync_state.now = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    token = single_part_token(harness, create_body())
    harness.sync_state.now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
    response = upload(harness, token, _CONTENT)
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "small_file_operation_expired"
    assert harness.sync_state.publication_commits == 0


# --- response hygiene ------------------------------------------------------------------------


def test_no_response_ever_carries_receipt_object_or_provider_fields(
    harness: SmallFileRouteHarness,
) -> None:
    token = single_part_token(harness, create_body())
    for response in (
        upload(harness, token, _CONTENT),
        preflight(harness, create_body()),
        preflight(harness, {**create_body(), "sha256": "ZZZ"}),
        upload(harness, "a" * 43, _CONTENT),
    ):
        rendered = response.text
        for marker in FORBIDDEN_FIELD_MARKERS:
            assert marker not in rendered


# --- the bounded content stream limiter --------------------------------------------------------


class _ChunkedStream:
    """Caller-owned async byte stream of fixed chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._remaining = list(chunks)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        if not self._remaining:
            raise StopAsyncIteration
        return self._remaining.pop(0)


class _AdvancingMonotonicClock:
    """Monotonic clock seam advancing by one step per call."""

    def __init__(self, step_seconds: float) -> None:
        self._step_seconds = step_seconds
        self._now = 0.0

    def __call__(self) -> float:
        self._now += self._step_seconds
        return self._now


async def _consume(stream: AsyncIterator[bytes]) -> tuple[bytes, Exception | None]:
    chunks: list[bytes] = []
    try:
        async for chunk in stream:
            chunks.append(chunk)
    except Exception as error:
        return b"".join(chunks), error
    return b"".join(chunks), None


@pytest.mark.asyncio
async def test_content_limiter_passes_a_stream_exactly_at_the_ceiling() -> None:
    maximum = 8
    stream = bounded_content_stream(
        _ChunkedStream([b"1234", b"5678"]),
        maximum_bytes=maximum,
        deadline_seconds=CONTENT_READ_DEADLINE_SECONDS,
        monotonic_clock=_AdvancingMonotonicClock(0.0),
    )
    content, error = await _consume(stream)
    assert error is None
    assert content == b"12345678"


@pytest.mark.asyncio
async def test_content_limiter_skips_zero_length_wire_chunks() -> None:
    """A proxied chunked body may carry zero-length data events (spec 10.2).

    The HTTP adapter owns wire normalization: an empty chunk carries no
    bytes and must never reach the spool path, whose per-chunk contract
    rejects empty chunks as malformed. Observed live through a Cloudflare
    tunnel PUT.
    """
    collected: list[bytes] = []
    error: Exception | None = None
    stream = bounded_content_stream(
        _ChunkedStream([b"12", b"", b"34", b"", b"5678"]),
        maximum_bytes=8,
        deadline_seconds=CONTENT_READ_DEADLINE_SECONDS,
        monotonic_clock=_AdvancingMonotonicClock(0.0),
    )
    try:
        async for chunk in stream:
            collected.append(chunk)
    except Exception as cause:  # asserted below
        error = cause
    assert error is None
    assert b"".join(collected) == b"12345678"
    assert b"" not in collected


@pytest.mark.asyncio
async def test_content_limiter_rejects_a_stream_over_the_ceiling() -> None:
    from personal_os.error_contracts.codes import ErrorCode
    from personal_os.small_file_sync.errors import SmallFileSyncError

    stream = bounded_content_stream(
        _ChunkedStream([b"1234", b"5678", b"9"]),
        maximum_bytes=8,
        deadline_seconds=CONTENT_READ_DEADLINE_SECONDS,
        monotonic_clock=_AdvancingMonotonicClock(0.0),
    )
    content, error = await _consume(stream)
    assert isinstance(error, SmallFileSyncError)
    assert error.error_code is ErrorCode.SMALL_FILE_SIZE_LIMIT_EXCEEDED
    assert content == b"12345678"


@pytest.mark.asyncio
async def test_content_limiter_rejects_a_stalled_stream_past_the_deadline() -> None:
    """A stream that stops yielding without erroring cannot block the read.

    The stall is a genuine never-resolving await after the first chunk — the
    deadline must bound the await itself, not only the moments a chunk
    happens to arrive, or a silent client would hold the handler forever.
    """

    import asyncio

    from personal_os.error_contracts.codes import ErrorCode
    from personal_os.small_file_sync.errors import SmallFileSyncError

    class _StalledStream:
        """Delivers one chunk, then awaits an event that is never set."""

        def __init__(self) -> None:
            self._first = True
            self._never = asyncio.Event()

        def __aiter__(self) -> AsyncIterator[bytes]:
            return self

        async def __anext__(self) -> bytes:
            if self._first:
                self._first = False
                return b"1234"
            await self._never.wait()
            raise AssertionError("the stalled read must never resolve")

    stream = bounded_content_stream(
        _StalledStream(),
        maximum_bytes=8,
        deadline_seconds=0.1,
        monotonic_clock=time.monotonic,
    )
    content, error = await _consume(stream)
    assert isinstance(error, SmallFileSyncError)
    assert error.error_code is ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED
    assert content == b"1234"


@pytest.mark.asyncio
async def test_content_limiter_rejects_a_stream_that_never_yields() -> None:
    """Even a stream that stalls before its first byte stays bounded."""

    import asyncio

    from personal_os.error_contracts.codes import ErrorCode
    from personal_os.small_file_sync.errors import SmallFileSyncError

    class _SilentStream:
        def __aiter__(self) -> AsyncIterator[bytes]:
            return self

        async def __anext__(self) -> bytes:
            await asyncio.Event().wait()
            raise AssertionError("the stalled read must never resolve")

    stream = bounded_content_stream(
        _SilentStream(),
        maximum_bytes=8,
        deadline_seconds=0.1,
        monotonic_clock=time.monotonic,
    )
    _, error = await _consume(stream)
    assert isinstance(error, SmallFileSyncError)
    assert error.error_code is ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED
