"""Multipart upload session routes over the offline composition (spec 5).

These tests drive the five closed endpoints — create-or-resume, status, one
part-URL issuance, completion and abort — through the real application
factory wired with the offline deterministic web-authentication and
multipart-upload compositions: no database, no provider client and no
environment read. Every route accepts exactly the ``obsidian_sync`` access
Bearer credential and derives workspace and device from it. The part-URL
response is the only surface a signed URL may appear on, is
``Cache-Control: no-store`` and never reappears on the status surface; a
foreign device observes the closed not-found token; the recheck policy guard
fails closed for a locator-keyed rule once the frozen update evidence drops
the locator; and the committed session's inline staging-delete failure
surfaces its closed reason token on the composition's rejection ring.
"""

from __future__ import annotations

from collections.abc import Iterator
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
from api_runtime.multipart_upload_composition import (
    OfflineMultipartUploadState,
    compose_offline_multipart_upload,
    multipart_recheck_locator_stand_in,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    RawPolicyDecision,
)
from personal_os.exclusion_policy.enforcement import (
    PolicyDecision,
)
from personal_os.multipart_upload.contracts import (
    MultipartPartGeometry,
    MultipartUploadSessionId,
)
from personal_os.multipart_upload.metrics import (
    MultipartMetricFlow,
    MultipartRejectionReason,
)
from personal_os.multipart_upload.service import derive_staging_key
from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.small_file_sync.contracts import (
    MAX_UPLOAD_FILE_SIZE_BYTES,
    NormalizedLocator,
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
)
from personal_os.sources.commands import SourceType
from personal_os.sources.reading import CanonicalSourceReference

ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN
_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}
#: A deterministic 20 MiB transfer: exactly three parts (8 MiB, 8 MiB, 4 MiB).
_MULTIPART_SIZE_BYTES: Final[int] = 20 * 1024 * 1024
_PREIMAGE: Final[bytes] = b"\x00" * _MULTIPART_SIZE_BYTES
_PREIMAGE_SHA256: Final[str] = sha256(_PREIMAGE).hexdigest()
_MEDIA_TYPE: Final[str] = "text/markdown"

_SESSIONS_PATH: Final[str] = "/api/uploads/multipart-sessions"
_PART_URL_MAX_PART_NUMBER: Final[int] = 13

#: Substrings no multipart response may ever contain: the presigned URL is
#: confined to the one part-URL response, and no provider identity, staging
#: key or signature fragment may render anywhere.
FORBIDDEN_RESPONSE_MARKERS: Final[tuple[str, ...]] = (
    "staging_key",
    "provider_upload_id",
    "upload_id",
    "etag",
    "signature",
    "X-Amz",
)


class _ReadyProbe:
    """Readiness probe stub: the multipart routes never consult dependencies."""

    async def check(self) -> None: ...


@dataclass(frozen=True, slots=True)
class MultipartRouteHarness:
    """One test client bound to the shared offline state and one device."""

    client: TestClient
    multipart_state: OfflineMultipartUploadState
    access_credential: str
    device_id: UUID


def _exchange_device_credential(client: TestClient, *, device_name: str) -> str:
    """Exchange one approved device grant through the real routes."""

    created = client.post(
        "/api/auth/device-authorizations",
        headers={"Origin": ORIGIN},
        json={
            "client_instance_id": str(uuid4()),
            "device_name": device_name,
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
    return str(exchanged.json()["data"]["access_credential"])


@pytest.fixture
def harness() -> Iterator[MultipartRouteHarness]:
    clock = OfflineAuthenticationClock()
    auth_state = OfflineAuthenticationState(totp_active=False)
    multipart_state = OfflineMultipartUploadState(staging_preimage=_PREIMAGE)
    application: FastAPI = create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(clock=clock, state=auth_state),
        multipart_upload=compose_offline_multipart_upload(state=multipart_state),
    )
    with TestClient(application, base_url=ORIGIN) as test_client:
        credential = _exchange_device_credential(test_client, device_name="Personal desktop")
        yield MultipartRouteHarness(
            client=test_client,
            multipart_state=multipart_state,
            access_credential=credential,
            device_id=uuid4(),
        )


def bearer(harness: MultipartRouteHarness) -> dict[str, str]:
    return {"Authorization": f"Bearer {harness.access_credential}"}


def create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "event_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "operation": "create",
        "local_file_id": str(uuid4()),
        "source_id": None,
        "base_version_id": None,
        "normalized_locator": "notes/large-note.md",
        "sha256": _PREIMAGE_SHA256,
        "size_bytes": _MULTIPART_SIZE_BYTES,
        "media_type": _MEDIA_TYPE,
        "policy_revision": 7,
    }
    body.update(overrides)
    return body


def update_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        **create_body(**overrides),
        "operation": "update",
        "source_id": str(uuid4()),
        "base_version_id": str(uuid4()),
    }
    body.update(overrides)
    return body


def create_session(
    harness: MultipartRouteHarness, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    response = harness.client.post(
        _SESSIONS_PATH, headers=bearer(harness), json=body if body is not None else create_body()
    )
    assert response.status_code == 200, response.text
    data = dict(response.json()["data"])
    assert set(data) == {"session_id", "part_size_bytes", "part_count", "expires_at"}, data
    assert response.headers["cache-control"] == "no-store"
    return data


def status_path(session_id: str) -> str:
    return f"{_SESSIONS_PATH}/{session_id}"


def part_url_path(session_id: str, part_number: int) -> str:
    return f"{_SESSIONS_PATH}/{session_id}/parts/{part_number}/url"


def complete_path(session_id: str) -> str:
    return f"{_SESSIONS_PATH}/{session_id}/complete"


def abort_path(session_id: str) -> str:
    return f"{_SESSIONS_PATH}/{session_id}/abort"


def _reference_for(body: dict[str, Any]) -> CanonicalSourceReference:
    return CanonicalSourceReference(
        workspace_id=uuid4(),
        source_id=UUID(str(body["source_id"])),
        source_version_id=UUID(str(body["base_version_id"])),
        content_version=3,
        source_type=SourceType.MARKDOWN,
        expected_object=ExpectedObject(
            content_digest=ContentDigest.parse(str(body["sha256"])),
            size_bytes=int(body["size_bytes"]),
            media_type=CanonicalMediaType.parse(str(body["media_type"])),
        ),
        committed_at=datetime(2026, 8, 28, 9, 30, 0, tzinfo=UTC),
    )


def _seed_all_parts(harness: MultipartRouteHarness, session_id: str, size_bytes: int) -> None:
    provider = harness.multipart_state.require_provider()
    staging_key = derive_staging_key(MultipartUploadSessionId(session_id))
    geometry = MultipartPartGeometry.from_size_bytes(size_bytes)
    for part_number in range(1, geometry.part_count + 1):
        provider.upload_part(staging_key, part_number, geometry.part_range(part_number).size_bytes)


# --- authentication gates --------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "with_body"),
    [
        ("post", _SESSIONS_PATH, True),
        ("get", f"{_SESSIONS_PATH}/{'a' * 32}", False),
        ("post", f"{_SESSIONS_PATH}/{'a' * 32}/parts/1/url", False),
        ("post", f"{_SESSIONS_PATH}/{'a' * 32}/complete", False),
        ("post", f"{_SESSIONS_PATH}/{'a' * 32}/abort", False),
    ],
)
def test_every_route_demands_the_device_access_credential(
    harness: MultipartRouteHarness, method: str, path: str, with_body: bool
) -> None:
    request = getattr(harness.client, method)
    missing = request(path, **({"json": update_body()} if with_body else {}))
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "device_credential_invalid"
    assert missing.headers["cache-control"] == "no-store"

    unknown = request(
        path,
        headers={"Authorization": f"Bearer at1.{uuid4()}.{bytes(range(32)).hex()}"},
        **({"json": update_body()} if with_body else {}),
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"]["code"] == "device_credential_invalid"


def test_session_cookie_cannot_authenticate_the_surface(
    harness: MultipartRouteHarness,
) -> None:
    login = harness.client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    session_only = harness.client.get(
        f"{_SESSIONS_PATH}/{'a' * 32}",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={login.cookies[SESSION_COOKIE_NAME]}"},
    )
    assert session_only.status_code == 401
    assert session_only.json()["error"]["code"] == "device_credential_invalid"


# --- create-or-resume ------------------------------------------------------------


def test_create_returns_the_server_owned_plan_geometry(harness: MultipartRouteHarness) -> None:
    data = create_session(harness)
    session_id = str(data["session_id"])
    assert 32 <= len(session_id) <= 128
    assert all(char.isalnum() or char in {"-", "_"} for char in session_id)
    assert data["part_size_bytes"] == 8 * 1024 * 1024
    assert data["part_count"] == 3
    assert data["expires_at"] is not None


def test_exact_create_replay_returns_the_same_single_session(
    harness: MultipartRouteHarness,
) -> None:
    body = create_body()
    first = create_session(harness, body)
    replay = create_session(harness, body)
    assert replay["session_id"] == first["session_id"]

    divergent_fingerprint = harness.client.post(
        _SESSIONS_PATH,
        headers=bearer(harness),
        json=create_body(
            event_id=body["event_id"], idempotency_key=body["idempotency_key"], sha256="1" * 64
        ),
    )
    assert divergent_fingerprint.status_code == 409
    assert divergent_fingerprint.json()["error"]["code"] == (
        "small_file_operation_identity_mismatch"
    )


@pytest.mark.parametrize(
    "body",
    [
        {**create_body(), "workspace_id": str(uuid4())},
        {**create_body(), "device_id": str(uuid4())},
        {**create_body(), "user_id": str(uuid4())},
        {**create_body(), "presigned_url": "https://staging.example.invalid/part"},
        {name: value for name, value in create_body().items() if name != "sha256"},
    ],
)
def test_create_rejects_non_contract_bodies(
    harness: MultipartRouteHarness, body: dict[str, Any]
) -> None:
    response = harness.client.post(_SESSIONS_PATH, headers=bearer(harness), json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] in {
        "api_request_validation_failed",
        "small_file_preflight_invalid",
    }


def test_create_rejects_sizes_outside_the_multipart_routing_range(
    harness: MultipartRouteHarness,
) -> None:
    single_part = harness.client.post(
        _SESSIONS_PATH,
        headers=bearer(harness),
        json=create_body(size_bytes=16 * 1024 * 1024, sha256="0" * 64),
    )
    assert single_part.status_code == 422
    assert single_part.json()["error"]["code"] == "multipart_part_invalid"

    over_maximum = harness.client.post(
        _SESSIONS_PATH,
        headers=bearer(harness),
        json=create_body(size_bytes=MAX_UPLOAD_FILE_SIZE_BYTES + 1, sha256="0" * 64),
    )
    assert over_maximum.status_code == 422
    assert over_maximum.json()["error"]["code"] == "small_file_size_limit_exceeded"


def test_denied_policy_closes_the_session_create(harness: MultipartRouteHarness) -> None:
    harness.multipart_state.is_policy_denied = True
    response = harness.client.post(_SESSIONS_PATH, headers=bearer(harness), json=create_body())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "multipart_policy_denied"
    assert response.headers["cache-control"] == "no-store"


# --- status -----------------------------------------------------------------------


def test_status_reconciles_provider_observed_completed_parts(
    harness: MultipartRouteHarness,
) -> None:
    data = create_session(harness)
    session_id = str(data["session_id"])
    _seed_all_parts(harness, session_id, _MULTIPART_SIZE_BYTES)
    response = harness.client.get(status_path(session_id), headers=bearer(harness))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = dict(response.json()["data"])
    assert set(body) == {
        "session_id",
        "state",
        "part_size_bytes",
        "part_count",
        "expires_at",
        "completed_part_numbers",
    }
    assert body["state"] == "uploading"
    assert body["completed_part_numbers"] == [1, 2, 3]


def test_part_url_response_is_no_store_and_not_returned_by_status(
    harness: MultipartRouteHarness,
) -> None:
    session_id = str(create_session(harness)["session_id"])
    issued = harness.client.post(part_url_path(session_id, 1), headers=bearer(harness))
    assert issued.status_code == 200, issued.text
    assert issued.headers["cache-control"] == "no-store"
    data = dict(issued.json()["data"])
    assert set(data) == {
        "part_number",
        "offset_bytes",
        "size_bytes",
        "url",
        "expires_at",
    }
    assert data["url"].startswith("https://")
    assert data["part_number"] == 1
    assert data["offset_bytes"] == 0
    status = harness.client.get(status_path(session_id), headers=bearer(harness))
    assert status.status_code == 200
    assert status.json()["data"].get("url") is None


def test_foreign_device_cannot_read_or_abort_session(
    harness: MultipartRouteHarness,
) -> None:
    session_id = str(create_session(harness)["session_id"])
    foreign_token = _exchange_device_credential(harness.client, device_name="Foreign desktop")
    for response in (
        harness.client.get(
            status_path(session_id), headers={"Authorization": f"Bearer {foreign_token}"}
        ),
        harness.client.post(
            abort_path(session_id), headers={"Authorization": f"Bearer {foreign_token}"}
        ),
        harness.client.post(
            part_url_path(session_id, 1), headers={"Authorization": f"Bearer {foreign_token}"}
        ),
    ):
        assert response.status_code in {403, 404}
        assert response.json()["error"]["code"] == "multipart_session_not_found"


@pytest.mark.parametrize(
    "session_id",
    ["short-token", "!!!invalid-session-ids-are-not-urlsafe!!!", "a" * 129],
)
def test_status_rejects_malformed_session_ids(
    harness: MultipartRouteHarness, session_id: str
) -> None:
    response = harness.client.get(status_path(session_id), headers=bearer(harness))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"


def test_status_rejects_the_raw_uuid_session_id_form(harness: MultipartRouteHarness) -> None:
    response = harness.client.get(
        status_path("00000000-0000-7000-8000-0000000000ab"), headers=bearer(harness)
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"


def test_status_rejects_unknown_sessions(harness: MultipartRouteHarness) -> None:
    response = harness.client.get(status_path("a" * 43), headers=bearer(harness))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "multipart_session_not_found"


# --- part URL issuance --------------------------------------------------------------


def test_part_url_rejects_numbers_outside_the_session_geometry(
    harness: MultipartRouteHarness,
) -> None:
    session_id = str(create_session(harness)["session_id"])
    beyond_geometry = harness.client.post(part_url_path(session_id, 4), headers=bearer(harness))
    assert beyond_geometry.status_code == 422
    assert beyond_geometry.json()["error"]["code"] == "multipart_part_invalid"

    beyond_wire_bound = harness.client.post(
        part_url_path(session_id, _PART_URL_MAX_PART_NUMBER + 1), headers=bearer(harness)
    )
    assert beyond_wire_bound.status_code == 422
    assert beyond_wire_bound.json()["error"]["code"] == "api_request_validation_failed"

    not_a_number = harness.client.post(
        part_url_path(session_id, 1).replace("/1/", "/one/"), headers=bearer(harness)
    )
    assert not_a_number.status_code == 422


def test_locator_keyed_policy_rule_fails_closed_on_the_update_recheck(
    harness: MultipartRouteHarness,
) -> None:
    """A locator-keyed deny cannot pass the update session's early rechecks."""

    body = update_body()
    harness.multipart_state.current_reference = _reference_for(body)
    session_id = str(create_session(harness, body)["session_id"])

    harness.multipart_state.locator_keyed_rule_present = True
    response = harness.client.post(part_url_path(session_id, 1), headers=bearer(harness))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "multipart_policy_denied"
    assert response.headers["cache-control"] == "no-store"


# --- completion ----------------------------------------------------------------------


def test_complete_publishes_once_and_freezes_the_terminal_result(
    harness: MultipartRouteHarness,
) -> None:
    session_id = str(create_session(harness)["session_id"])
    _seed_all_parts(harness, session_id, _MULTIPART_SIZE_BYTES)
    completed = harness.client.post(complete_path(session_id), headers=bearer(harness))
    assert completed.status_code == 200, completed.text
    assert completed.headers["cache-control"] == "no-store"
    data = dict(completed.json()["data"])
    assert set(data) == {"state", "terminal_result"}
    assert data["state"] == "committed"
    assert set(data["terminal_result"]) == {
        "result_kind",
        "source_id",
        "source_version_id",
        "content_version",
        "committed_at",
    }
    assert harness.multipart_state.publication_commits == 1

    replay = harness.client.post(complete_path(session_id), headers=bearer(harness))
    assert replay.status_code == 200
    assert replay.json()["data"] == data
    assert harness.multipart_state.publication_commits == 1


def test_complete_without_every_part_fails_closed_without_publishing(
    harness: MultipartRouteHarness,
) -> None:
    session_id = str(create_session(harness)["session_id"])
    response = harness.client.post(complete_path(session_id), headers=bearer(harness))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "multipart_provider_state_invalid"
    assert harness.multipart_state.publication_commits == 0
    aborted_after_failure = harness.client.get(status_path(session_id), headers=bearer(harness))
    assert aborted_after_failure.status_code == 200
    assert aborted_after_failure.json()["data"]["state"] == "integrity_failed"


def test_committed_inline_staging_delete_failure_surfaces_its_closed_reason(
    harness: MultipartRouteHarness,
) -> None:
    """The D2 surface: a failed inline delete never fails the frozen result.

    The composition's rejection ring is the readable reason surface for this
    one path: the closed cleanup token must be observable together with the
    completion flow, while the committed receipt still returns unchanged.
    """

    harness.multipart_state.delete_staging_error_reason = (
        MultipartRejectionReason.MULTIPART_CLEANUP_FAILED
    )
    session_id = str(create_session(harness)["session_id"])
    _seed_all_parts(harness, session_id, _MULTIPART_SIZE_BYTES)
    completed = harness.client.post(complete_path(session_id), headers=bearer(harness))
    assert completed.status_code == 200
    assert completed.json()["data"]["state"] == "committed"

    diagnostics = harness.multipart_state.rejection_diagnostics_snapshot()
    assert (
        diagnostics.rejection_counters.get(
            (MultipartMetricFlow.COMPLETION, MultipartRejectionReason.MULTIPART_CLEANUP_FAILED),
            0,
        )
        >= 1
    )


# --- cancellation ----------------------------------------------------------------------


def test_abort_terminalizes_cancellation_and_replays_idempotently(
    harness: MultipartRouteHarness,
) -> None:
    session_id = str(create_session(harness)["session_id"])
    aborted = harness.client.post(abort_path(session_id), headers=bearer(harness))
    assert aborted.status_code == 200
    assert aborted.headers["cache-control"] == "no-store"
    assert aborted.json()["data"]["state"] == "cancelling"
    replay = harness.client.post(abort_path(session_id), headers=bearer(harness))
    assert replay.status_code == 200
    assert replay.json()["data"]["state"] == "cancelling"

    url_after_abort = harness.client.post(part_url_path(session_id, 1), headers=bearer(harness))
    assert url_after_abort.status_code == 409
    assert url_after_abort.json()["error"]["code"] == "multipart_session_state_invalid"


# --- response hygiene --------------------------------------------------------------------


def test_no_response_ever_carries_provider_or_signature_material(
    harness: MultipartRouteHarness,
) -> None:
    session_id = str(create_session(harness)["session_id"])
    for response in (
        harness.client.get(status_path(session_id), headers=bearer(harness)),
        harness.client.post(part_url_path(session_id, 1), headers=bearer(harness)),
        harness.client.post(complete_path(session_id), headers=bearer(harness)),
        harness.client.post(abort_path("a" * 43), headers=bearer(harness)),
        harness.client.get(status_path("a" * 43), headers=bearer(harness)),
    ):
        if response.request.url.path.endswith("/url") and response.status_code == 200:
            continue  # the one part-URL response is the sole signed-URL surface
        for marker in FORBIDDEN_RESPONSE_MARKERS:
            assert marker not in response.text, marker


# --- the composition seams ----------------------------------------------------------------


class _RecordingPolicyEnforcement:
    """Enforcement stub recording the subject evidence of each evaluation.

    Simulates one locator-keyed rule: a locator-free subject observes
    indeterminate evidence (the closed fail-closed evaluation of the real
    enforcement service), a locator-carrying subject is allowed.
    """

    def __init__(self) -> None:
        self.subjects: list[Any] = []

    async def authorize_preflight(
        self,
        *,
        subject: Any,
        boundary: Any,
        context: DiagnosticContext,
    ) -> PolicyDecision:
        del boundary, context
        from personal_os.exclusion_policy.errors import ExclusionPolicyError

        self.subjects.append(subject)
        if subject.normalized_locator is None:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_INDETERMINATE)
        # The real enforcement service answers with its internal decision
        # evidence; the guard converts it to the server-owned binding.
        return PolicyDecision(
            workspace_id=subject.workspace_id,
            policy_revision_id=uuid4(),
            revision_number=7,
            subject_fingerprint=b"0" * 32,
            raw_decision=RawPolicyDecision.ALLOWED,
            enforced_decision=EnforcedPolicyDecision.ALLOWED,
            matched_rule_ids=(),
            missing_fields=(),
            evaluated_at=datetime.now(UTC),
        )


def _recheck_preflight(locator: NormalizedLocator) -> SmallFilePreflight:
    return SmallFilePreflight(
        event_id=uuid4(),
        idempotency_key=SmallFileIdempotencyKey(str(uuid4())),
        operation=SmallFileOperation.UPDATE,
        local_file_id=uuid4(),
        source_id=uuid4(),
        base_version_id=uuid4(),
        normalized_locator=locator,
        sha256=ContentDigest.parse(_PREIMAGE_SHA256),
        size_bytes=_MULTIPART_SIZE_BYTES,
        media_type=CanonicalMediaType.parse(_MEDIA_TYPE),
        policy_revision_number=7,
    )


@pytest.mark.asyncio
async def test_recheck_guard_evaluates_the_stand_in_subject_locator_free() -> None:
    """The parked finding D1 resolution: the recheck guard fails closed.

    A frozen update recheck carries the service's fixed locator stand-in; the
    guard derives that stand-in from the reconstruction function itself and
    evaluates the subject locator-free, so a locator-keyed rule observes
    indeterminate evidence and the boundary fails closed instead of passing
    an early recheck with a fabricated locator.
    """

    from api_runtime.multipart_upload_composition import (
        RecheckLocatorAwarePolicyEnforcementGuard,
    )

    from personal_os.exclusion_policy.errors import ExclusionPolicyError

    enforcement = _RecordingPolicyEnforcement()
    guard = RecheckLocatorAwarePolicyEnforcementGuard(enforcement=enforcement)
    stand_in = multipart_recheck_locator_stand_in()
    assert isinstance(stand_in, NormalizedLocator)

    with pytest.raises(ExclusionPolicyError) as denied:
        await guard.authorize_small_file(
            _recheck_preflight(stand_in),
            _device_context(),
            _diagnostic_context(),
        )
    assert denied.value.error_code is ErrorCode.EXCLUSION_POLICY_INDETERMINATE
    evaluated = enforcement.subjects[-1]
    assert evaluated.normalized_locator is None

    binding = await guard.authorize_small_file(
        _recheck_preflight(NormalizedLocator("notes/large-note.md")),
        _device_context(),
        _diagnostic_context(),
    )
    assert binding.policy_revision_number == 7
    assert enforcement.subjects[-1].normalized_locator == "notes/large-note.md"


def _device_context() -> SmallFileDeviceContext:
    return SmallFileDeviceContext(device_id=uuid4(), workspace_id=uuid4())


def _diagnostic_context() -> DiagnosticContext:
    return create_diagnostic_context().context


def test_offline_runtime_binds_the_rejection_ring_read_side() -> None:
    """The parked finding D2 surface: the ring read side is always bound."""

    runtime = compose_offline_multipart_upload()
    assert runtime.rejection_diagnostics is not None
    assert runtime.rejection_diagnostics.rejection_diagnostics().rejection_counters == {}
