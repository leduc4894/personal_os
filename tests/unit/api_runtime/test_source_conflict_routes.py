"""Source conflict routes over the offline composition (Child 8 spec 6).

These tests drive the four closed Conflict Inbox routes — the open-conflict
listing, the safe detail with its choices, the policy-rechecked verified
evidence stream and the idempotent resolve — through the real application
factory wired with the offline deterministic web-authentication and
source-conflict compositions: no database, no key file, no provider client
and no environment read. Every route accepts exactly the ``obsidian_sync``
access Bearer credential and derives the workspace from it — no request
field ever selects one, and a conflict of another workspace is
indistinguishable from missing. JSON responses carry the canonical envelope
and ``Cache-Control: no-store``; the binary evidence success carries the
exact canonical ``Content-Type`` and ``Content-Length`` headers, and the
evidence reader only opens after the policy recheck allowed the read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
from api_runtime.source_conflict_composition import (
    OfflineConflictEvidenceReader,
    OfflineSourceConflictState,
    compose_offline_source_conflicts,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_os.error_contracts.codes import ErrorCode
from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.source_conflicts.commands import ConflictResolutionResult
from personal_os.source_conflicts.contracts import (
    ConflictCandidate,
    ConflictEvidenceRole,
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
    ConflictStatus,
    SourceConflict,
)
from personal_os.source_conflicts.errors import SourceConflictError

ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN
_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}
#: The fixed workspace of the offline web-authentication graph: every
#: exchanged device credential derives this workspace, so the seeded
#: conflicts belong to it.
_WORKSPACE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000002")
_CONFLICT_ID: Final[UUID] = uuid4()
_CAPTURED_AT: Final[datetime] = datetime(2026, 9, 2, 9, 15, 0, tzinfo=UTC)
_OFFLINE_EVIDENCE: Final[bytes] = b"offline source conflict evidence bytes"

#: The eight closed source-conflict codes and their registered statuses.
CONFLICT_ERROR_STATUSES: Final[dict[ErrorCode, int]] = {
    ErrorCode.SOURCE_CONFLICT_INPUT_INVALID: 422,
    ErrorCode.SOURCE_CONFLICT_NOT_FOUND: 404,
    ErrorCode.SOURCE_CONFLICT_STATE_INVALID: 409,
    ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH: 409,
    ErrorCode.SOURCE_CONFLICT_EVIDENCE_UNAVAILABLE: 404,
    ErrorCode.SOURCE_CONFLICT_EVIDENCE_INTEGRITY_FAILED: 422,
    ErrorCode.SOURCE_CONFLICT_DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.SOURCE_CONFLICT_COMMIT_OUTCOME_UNKNOWN: 503,
}

#: Substrings no source conflict response may ever contain.
FORBIDDEN_FIELD_MARKERS: Final[tuple[str, ...]] = (
    "object_key",
    "receipt",
    "presign",
    "bucket",
    "provider",
    "secret",
)


class _ReadyProbe:
    """Readiness probe stub: the source conflict routes never consult it."""

    async def check(self) -> None: ...


def _conflict(**overrides: Any) -> SourceConflict:
    fields: dict[str, Any] = dict(
        conflict_id=_CONFLICT_ID,
        workspace_id=_WORKSPACE_ID,
        source_id=uuid4(),
        conflict_kind=ConflictKind.STALE_CONTENT,
        status=ConflictStatus.OPEN,
        originating_event_id=uuid4(),
        originating_device_id=uuid4(),
        base_version_id=uuid4(),
        observed_remote_version_id=uuid4(),
        candidate=ConflictCandidate.content(uuid4()),
        captured_at=_CAPTURED_AT,
        resolution_kind=None,
        resolution_event_id=None,
        resulting_version_id=None,
        successor_conflict_id=None,
        closed_at=None,
    )
    fields.update(overrides)
    return SourceConflict(**fields)


def _resolve_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "resolution_event_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "resolution_kind": "keep_remote",
        "reviewed_remote_version_id": str(uuid4()),
        "verified_candidate_object_id": None,
    }
    body.update(overrides)
    return body


@dataclass(frozen=True, slots=True)
class SourceConflictRouteHarness:
    """One test client bound to the shared offline state and one device."""

    client: TestClient
    state: OfflineSourceConflictState
    guarded_reader: OfflineConflictEvidenceReader
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
def harness() -> Any:
    clock = OfflineAuthenticationClock()
    auth_state = OfflineAuthenticationState(totp_active=False)
    conflict_state = OfflineSourceConflictState()
    runtime = compose_offline_source_conflicts(state=conflict_state)
    application: FastAPI = create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(clock=clock, state=auth_state),
        source_conflicts=runtime,
    )
    with TestClient(application, base_url=ORIGIN) as test_client:
        credential, device_id = _exchange_device_credential(test_client)
        yield SourceConflictRouteHarness(
            client=test_client,
            state=conflict_state,
            guarded_reader=runtime.evidence,  # type: ignore[assignment]
            access_credential=credential,
            device_id=device_id,
        )


def bearer(harness: SourceConflictRouteHarness) -> dict[str, str]:
    return {"Authorization": f"Bearer {harness.access_credential}"}


# --- the brief's two mandated snippets -----------------------------------------------------------


def test_resolve_requires_device_credential_and_rejects_raw_merged_bytes(
    harness: SourceConflictRouteHarness,
) -> None:
    response = harness.client.post(
        "/api/sync/conflicts/" + str(_CONFLICT_ID) + "/resolve", json={"raw": "secret"}
    )
    assert response.status_code in {401, 422}

    authenticated = harness.client.post(
        "/api/sync/conflicts/" + str(_CONFLICT_ID) + "/resolve",
        headers=bearer(harness),
        json={"raw": "secret"},
    )
    assert authenticated.status_code == 422
    assert authenticated.json()["error"]["code"] == "api_request_validation_failed"


def test_evidence_download_rechecks_policy_before_opening_reader(
    harness: SourceConflictRouteHarness,
) -> None:
    harness.state.open_conflicts = (_conflict(),)
    harness.state.is_policy_denied = True
    response = harness.client.get(
        f"/api/sync/conflicts/{_CONFLICT_ID}/evidence/base", headers=bearer(harness)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "exclusion_policy_denied"
    assert harness.guarded_reader.open_count == 0


# --- authentication gates ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/sync/conflicts"),
        ("get", f"/api/sync/conflicts/{uuid4()}"),
        ("get", f"/api/sync/conflicts/{uuid4()}/evidence/base"),
        ("post", f"/api/sync/conflicts/{uuid4()}/resolve"),
    ],
)
def test_every_route_demands_the_device_access_credential(
    harness: SourceConflictRouteHarness, method: str, path: str
) -> None:
    request = getattr(harness.client, method)
    kwargs: dict[str, Any] = {"json": _resolve_body()} if method == "post" else {}

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
        path, headers={"Authorization": f"Bearer at1.{uuid4()}.{bytes(range(32)).hex()}"}, **kwargs
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"]["code"] == "device_credential_invalid"


# --- the open-conflict listing -------------------------------------------------------------------


def test_list_returns_the_seeded_open_conflicts_with_safe_metadata(
    harness: SourceConflictRouteHarness,
) -> None:
    harness.state.open_conflicts = (
        _conflict(),
        _conflict(
            conflict_id=uuid4(),
            conflict_kind=ConflictKind.DELETE_REMOTE_EDIT,
            candidate=ConflictCandidate.delete(),
        ),
    )
    response = harness.client.get("/api/sync/conflicts", headers=bearer(harness))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert set(payload) == {"request_id", "data", "warnings", "error"}
    data = payload["data"]
    assert set(data) == {"conflicts", "has_more", "next_exclusive_start_conflict_id"}
    assert len(data["conflicts"]) == 2
    first = data["conflicts"][0]
    assert set(first) == {
        "conflict_id",
        "source_id",
        "conflict_kind",
        "status",
        "originating_event_id",
        "originating_device_id",
        "base_version_id",
        "observed_remote_version_id",
        "candidate_kind",
        "verified_candidate_object_id",
        "captured_at",
        "resolution_kind",
        "resolution_event_id",
        "resulting_version_id",
        "successor_conflict_id",
        "closed_at",
    }
    assert first["status"] == "open"
    assert data["has_more"] is False
    assert data["next_exclusive_start_conflict_id"] is None
    # The credential workspace scoped the store call; no request field picked it.
    (workspace_id, limit, exclusive_start) = harness.state.list_calls[0]
    assert workspace_id != UUID(int=0)
    assert limit == 50
    assert exclusive_start is None


def test_list_passes_the_pagination_knobs_and_bounds_the_page(
    harness: SourceConflictRouteHarness,
) -> None:
    harness.state.open_conflicts = (_conflict(),)
    # The nil identity sorts before every conflict id, so the seeded page
    # is deterministically inside the cursor window.
    exclusive_start = UUID(int=0)
    response = harness.client.get(
        "/api/sync/conflicts",
        headers=bearer(harness),
        params={"limit": 1, "exclusive_start_conflict_id": str(exclusive_start)},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["has_more"] is True
    assert data["next_exclusive_start_conflict_id"] == str(_CONFLICT_ID)
    assert harness.state.list_calls[0] == (harness.state.list_calls[0][0], 1, exclusive_start)


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"limit": -5}])
def test_list_rejects_out_of_bound_queries(
    harness: SourceConflictRouteHarness, params: dict[str, int]
) -> None:
    response = harness.client.get("/api/sync/conflicts", headers=bearer(harness), params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"


def test_list_maps_the_typed_store_errors_to_their_registered_envelopes(
    harness: SourceConflictRouteHarness,
) -> None:
    for error_code, status in CONFLICT_ERROR_STATUSES.items():
        harness.state.read_error = None
        harness.state.list_error = SourceConflictError(error_code)
        response = harness.client.get("/api/sync/conflicts", headers=bearer(harness))
        assert response.status_code == status, error_code
        assert response.json()["error"]["code"] == error_code.value
    harness.state.list_error = None


# --- the safe detail --------------------------------------------------------------------------


def test_detail_returns_the_safe_metadata_choices_and_evidence_identifiers(
    harness: SourceConflictRouteHarness,
) -> None:
    harness.state.open_conflicts = (_conflict(),)
    response = harness.client.get(f"/api/sync/conflicts/{_CONFLICT_ID}", headers=bearer(harness))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["conflict_id"] == str(_CONFLICT_ID)
    assert data["choices"] == ["keep_remote", "keep_local", "save_merged"]
    assert data["candidate_kind"] == "content"
    assert data["verified_candidate_object_id"] is not None
    # The evidence descriptors resolved inside the credential workspace.
    (conflict_id, role, workspace_id) = harness.state.describe_calls[0]
    assert conflict_id == _CONFLICT_ID
    assert role is ConflictEvidenceRole.CANDIDATE
    assert workspace_id != UUID(int=0)


def test_detail_never_offers_an_unappliable_choice_for_a_byteless_candidate(
    harness: SourceConflictRouteHarness,
) -> None:
    harness.state.open_conflicts = (
        _conflict(
            conflict_kind=ConflictKind.DELETE_REMOTE_EDIT,
            candidate=ConflictCandidate.delete(),
        ),
    )
    response = harness.client.get(f"/api/sync/conflicts/{_CONFLICT_ID}", headers=bearer(harness))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["candidate_kind"] == "delete"
    assert data["choices"] == ["keep_remote"]
    # No candidate evidence was ever resolved for a byteless conflict.
    assert harness.state.describe_calls == []


def test_detail_offers_no_merge_choice_for_binary_candidates(
    harness: SourceConflictRouteHarness,
) -> None:
    harness.state.open_conflicts = (_conflict(),)
    harness.state.evidence_media_type = "application/octet-stream"
    response = harness.client.get(f"/api/sync/conflicts/{_CONFLICT_ID}", headers=bearer(harness))
    assert response.status_code == 200
    assert response.json()["data"]["choices"] == ["keep_remote", "keep_local"]


def test_detail_answers_an_unknown_or_cross_workspace_conflict_not_found(
    harness: SourceConflictRouteHarness,
) -> None:
    missing = harness.client.get(f"/api/sync/conflicts/{uuid4()}", headers=bearer(harness))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "source_conflict_not_found"

    other_workspace = _conflict(workspace_id=uuid4())
    harness.state.open_conflicts = (other_workspace,)
    foreign = harness.client.get(
        f"/api/sync/conflicts/{other_workspace.conflict_id}", headers=bearer(harness)
    )
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "source_conflict_not_found"


def test_detail_rejects_malformed_conflict_identities(
    harness: SourceConflictRouteHarness,
) -> None:
    response = harness.client.get("/api/sync/conflicts/not-a-uuid", headers=bearer(harness))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"


# --- the verified evidence stream -----------------------------------------------------------------


def test_evidence_streams_verified_bytes_with_exact_headers_after_authorization(
    harness: SourceConflictRouteHarness,
) -> None:
    harness.state.open_conflicts = (_conflict(),)
    response = harness.client.get(
        f"/api/sync/conflicts/{_CONFLICT_ID}/evidence/remote", headers=bearer(harness)
    )
    assert response.status_code == 200
    # The exact canonical media type renders verbatim, with the exact byte
    # length; the evidence bytes carry the strictest cache posture.
    assert response.headers["content-type"] == "text/markdown"
    assert response.headers["content-length"] == str(len(_OFFLINE_EVIDENCE))
    assert response.headers["cache-control"] == "no-store, no-transform"
    assert response.content == _OFFLINE_EVIDENCE
    assert harness.guarded_reader.open_count == 1
    # The policy recheck over the credential workspace preceded the stream.
    assert harness.state.authorize_calls == [_CONFLICT_ID]
    (conflict_id, role, workspace_id) = harness.state.describe_calls[-1]
    assert (conflict_id, role) == (_CONFLICT_ID, ConflictEvidenceRole.REMOTE)
    assert workspace_id != UUID(int=0)


def test_evidence_role_is_a_closed_vocabulary(
    harness: SourceConflictRouteHarness,
) -> None:
    response = harness.client.get(
        f"/api/sync/conflicts/{_CONFLICT_ID}/evidence/notarole", headers=bearer(harness)
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"
    assert harness.guarded_reader.open_count == 0


def test_evidence_pre_stream_failures_remain_json_envelopes(
    harness: SourceConflictRouteHarness,
) -> None:
    harness.state.open_conflicts = (_conflict(),)

    harness.state.evidence_unavailable_roles = frozenset({ConflictEvidenceRole.CANDIDATE})
    unavailable = harness.client.get(
        f"/api/sync/conflicts/{_CONFLICT_ID}/evidence/candidate", headers=bearer(harness)
    )
    assert unavailable.status_code == 404
    assert unavailable.json()["error"]["code"] == "source_conflict_evidence_unavailable"
    assert harness.guarded_reader.open_count == 0

    harness.state.evidence_unavailable_roles = frozenset()
    harness.state.evidence_error = SourceConflictError(
        ErrorCode.SOURCE_CONFLICT_EVIDENCE_INTEGRITY_FAILED
    )
    corrupt = harness.client.get(
        f"/api/sync/conflicts/{_CONFLICT_ID}/evidence/base", headers=bearer(harness)
    )
    assert corrupt.status_code == 422
    assert corrupt.json()["error"]["code"] == "source_conflict_evidence_integrity_failed"
    assert harness.guarded_reader.open_count == 1

    harness.state.evidence_error = None
    harness.state.read_error = SourceConflictError(ErrorCode.SOURCE_CONFLICT_NOT_FOUND)
    missing = harness.client.get(
        f"/api/sync/conflicts/{_CONFLICT_ID}/evidence/base", headers=bearer(harness)
    )
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/json")
    harness.state.read_error = None


def test_evidence_answers_an_unknown_conflict_before_any_authorization(
    harness: SourceConflictRouteHarness,
) -> None:
    response = harness.client.get(
        f"/api/sync/conflicts/{uuid4()}/evidence/base", headers=bearer(harness)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "source_conflict_not_found"
    assert harness.guarded_reader.open_count == 0
    assert harness.state.authorize_calls == []


# --- the idempotent resolve ------------------------------------------------------------------


def test_resolve_commits_the_winner_and_renders_the_typed_outcome(
    harness: SourceConflictRouteHarness,
) -> None:
    harness.state.open_conflicts = (_conflict(),)
    response = harness.client.post(
        f"/api/sync/conflicts/{_CONFLICT_ID}/resolve",
        headers=bearer(harness),
        json=_resolve_body(
            resolution_kind="save_merged", verified_candidate_object_id=str(uuid4())
        ),
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert set(data) == {
        "outcome",
        "conflict_id",
        "resolution_event_id",
        "resolution_kind",
        "resulting_version_id",
        "successor_conflict_id",
        "completed_at",
    }
    assert data["outcome"] == "resolved"
    assert data["resolution_kind"] == "save_merged"
    assert data["resulting_version_id"] is not None
    (command, workspace_id) = harness.state.resolve_calls[0]
    assert command.conflict_id == _CONFLICT_ID
    assert command.resolution_kind is ConflictResolutionKind.SAVE_MERGED
    assert workspace_id != UUID(int=0)
    # The row-locked read and the policy recheck both scoped the attempt.
    assert harness.state.authorize_calls == [_CONFLICT_ID]


def test_resolve_renders_the_stale_successor_outcome(
    harness: SourceConflictRouteHarness,
) -> None:
    harness.state.open_conflicts = (_conflict(),)
    successor = _conflict(conflict_id=uuid4(), observed_remote_version_id=uuid4())
    harness.state.resolve_result = ConflictResolutionResult(
        kind=ConflictResolutionOutcome.STALE_SUCCESSOR,
        conflict_id=_CONFLICT_ID,
        resolution_event_id=uuid4(),
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
        resulting_version_id=None,
        successor=successor,
        completed_at=datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC),
    )
    response = harness.client.post(
        f"/api/sync/conflicts/{_CONFLICT_ID}/resolve",
        headers=bearer(harness),
        json=_resolve_body(resolution_kind="keep_local"),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["outcome"] == "stale_successor"
    assert data["resulting_version_id"] is None
    assert data["successor_conflict_id"] == str(successor.conflict_id)


def test_resolve_maps_every_typed_error_to_its_registered_envelope(
    harness: SourceConflictRouteHarness,
) -> None:
    harness.state.open_conflicts = (_conflict(),)
    for error_code, status in CONFLICT_ERROR_STATUSES.items():
        harness.state.resolve_error = SourceConflictError(error_code)
        response = harness.client.post(
            f"/api/sync/conflicts/{_CONFLICT_ID}/resolve",
            headers=bearer(harness),
            json=_resolve_body(),
        )
        assert response.status_code == status, error_code
        body = response.json()
        assert body["error"]["code"] == error_code.value
        assert response.headers["cache-control"] == "no-store"
        is_retryable = body["error"]["retryable"]
        expected_retryable = error_code in {
            ErrorCode.SOURCE_CONFLICT_DEPENDENCY_UNAVAILABLE,
            ErrorCode.SOURCE_CONFLICT_COMMIT_OUTCOME_UNKNOWN,
        }
        assert is_retryable is expected_retryable, error_code
    harness.state.resolve_error = None


@pytest.mark.parametrize(
    "body",
    [
        {"raw": "secret"},
        {"resolution_kind": "keep_remote"},
        _resolve_body(resolution_event_id=str(UUID(int=0))),
        _resolve_body(idempotency_key=str(UUID(int=0))),
        _resolve_body(reviewed_remote_version_id=str(UUID(int=0))),
        _resolve_body(idempotency_key="not-a-uuid"),
        _resolve_body(resolution_kind="merge_theirs"),
        _resolve_body(resolution_kind="save_merged", verified_candidate_object_id=None),
        _resolve_body(verified_candidate_object_id=str(UUID(int=0))),
        {**_resolve_body(), "workspace_id": str(uuid4())},
        {**_resolve_body(), "merged_bytes": "c2VjcmV0"},
    ],
)
def test_resolve_rejects_non_contract_bodies(
    harness: SourceConflictRouteHarness, body: dict[str, Any]
) -> None:
    harness.state.open_conflicts = (_conflict(),)
    response = harness.client.post(
        f"/api/sync/conflicts/{_CONFLICT_ID}/resolve",
        headers=bearer(harness),
        json=body,
    )
    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] in {
        "api_request_validation_failed",
        "source_conflict_input_invalid",
    }
    assert harness.state.resolve_calls == []


def test_resolve_rejects_the_nil_conflict_identity(
    harness: SourceConflictRouteHarness,
) -> None:
    response = harness.client.post(
        "/api/sync/conflicts/00000000-0000-0000-0000-000000000000/resolve",
        headers=bearer(harness),
        json=_resolve_body(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "source_conflict_input_invalid"


# --- response hygiene ------------------------------------------------------------------------


def test_no_response_ever_carries_key_receipt_or_provider_fields(
    harness: SourceConflictRouteHarness,
) -> None:
    harness.state.open_conflicts = (_conflict(),)
    harness.state.resolve_error = SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID)
    responses = (
        harness.client.get("/api/sync/conflicts", headers=bearer(harness)),
        harness.client.get(f"/api/sync/conflicts/{_CONFLICT_ID}", headers=bearer(harness)),
        harness.client.get(
            f"/api/sync/conflicts/{_CONFLICT_ID}/evidence/candidate", headers=bearer(harness)
        ),
        harness.client.post(
            f"/api/sync/conflicts/{_CONFLICT_ID}/resolve",
            headers=bearer(harness),
            json=_resolve_body(),
        ),
    )
    for response in responses:
        rendered = response.text
        for marker in FORBIDDEN_FIELD_MARKERS:
            assert marker not in rendered


def test_every_json_response_carries_the_bound_request_id(
    harness: SourceConflictRouteHarness,
) -> None:
    harness.state.open_conflicts = (_conflict(),)
    response = harness.client.get("/api/sync/conflicts", headers=bearer(harness))
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_offline_defaults_are_an_empty_first_page(harness: SourceConflictRouteHarness) -> None:
    response = harness.client.get("/api/sync/conflicts", headers=bearer(harness))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["conflicts"] == []
    assert data["has_more"] is False
