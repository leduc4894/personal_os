"""Closed policy diagnostics admin route: membership and closed shape.

These tests compose the real application over the offline web-authentication
and exclusion-policy runtimes sharing one metrics recorder, then pin the one
authenticated read-only Admin route of the policy observability contract
(spec 2026-08-24 C2): exact method and semantic operation id in the document,
the strict Web session gate (device credentials and anonymous calls close
with the registered authentication error and the no-store posture), and the
closed payload — evaluation counters by boundary and decision (``failed``
included), publication outcome counters, and a bounded ring of the last
fifty policy system failures carrying only the closed registry error code,
the closed boundary label and the epoch-millisecond timestamp. A real
publication through the Admin routes proves the counters record actual
journeys; no path, locator, digest, operand or free-form string can appear
in the payload.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
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
from api_runtime.authentication_dependencies import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from api_runtime.exclusion_policy_composition import (
    OfflineExclusionPolicyState,
    compose_offline_exclusion_policy,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.metrics import (
    EvaluationMetricOutcome,
    InMemoryExclusionPolicyMetrics,
    PolicyBoundary,
    PublicationMetricOutcome,
)
from personal_os.exclusion_policy.previews import (
    PREVIEW_READY_EXPIRY_SECONDS,
    PreviewStatus,
    compute_impact_digest,
)
from personal_os.runtime_configuration.models import RuntimeEnvironment

ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN
_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}
_ROUTE_PATH: Final[str] = "/api/admin/exclusion-policy/diagnostics"
_FIRST_EPOCH_MS: Final[int] = 1_800_000_000_000
_REJECTED_OPERAND_SENTINEL: Final[str] = "/absolute/escape/path/do-not-emit"


class _SteppingEpochClock:
    """Deterministic epoch-ms seam: every call advances by one millisecond."""

    def __init__(self, first_epoch_ms: int = _FIRST_EPOCH_MS) -> None:
        self._next_epoch_ms = first_epoch_ms

    def __call__(self) -> int:
        current = self._next_epoch_ms
        self._next_epoch_ms += 1
        return current


class _ReadyProbe:
    """Readiness probe stub: the diagnostics route never consults it."""

    async def check(self) -> None: ...


@pytest.fixture
def recorder() -> InMemoryExclusionPolicyMetrics:
    return InMemoryExclusionPolicyMetrics(epoch_ms_clock=_SteppingEpochClock())


@pytest.fixture
def policy_state() -> OfflineExclusionPolicyState:
    return OfflineExclusionPolicyState()


@pytest.fixture
def application(
    recorder: InMemoryExclusionPolicyMetrics, policy_state: OfflineExclusionPolicyState
) -> FastAPI:
    return create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(
            clock=OfflineAuthenticationClock(),
            state=OfflineAuthenticationState(totp_active=False),
        ),
        exclusion_policy=compose_offline_exclusion_policy(state=policy_state, metrics=recorder),
    )


@pytest.fixture
def client(application: FastAPI) -> Iterator[TestClient]:
    with TestClient(application, base_url=ORIGIN) as test_client:
        yield test_client


def _admin_session_headers(client: TestClient) -> dict[str, str]:
    login = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    assert login.status_code == 200, login.text
    return {
        "Origin": ORIGIN,
        "Cookie": (
            f"{SESSION_COOKIE_NAME}={login.cookies[SESSION_COOKIE_NAME]}; "
            f"{CSRF_COOKIE_NAME}={login.cookies[CSRF_COOKIE_NAME]}"
        ),
        "X-CSRF-Token": login.cookies[CSRF_COOKIE_NAME],
    }


def _seed_closed_evidence(recorder: InMemoryExclusionPolicyMetrics) -> None:
    recorder.record_evaluation(
        boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
        decision=EvaluationMetricOutcome.ALLOWED,
        duration_seconds=0.01,
    )
    recorder.record_evaluation(
        boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
        decision=EvaluationMetricOutcome.FAILED,
        duration_seconds=0.02,
        error_code=ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE,
    )
    recorder.record_publication(outcome=PublicationMetricOutcome.REJECTED, duration_seconds=0.3)


def _publish_one_policy_revision(
    client: TestClient, policy_state: OfflineExclusionPolicyState
) -> None:
    """Drive exactly one committed publication through the real Admin routes."""

    headers = _admin_session_headers(client)
    preview = client.post("/api/admin/exclusion-policy/previews", headers=headers)
    assert preview.status_code == 202, preview.text
    record = _force_ready_preview(preview, policy_state)
    published = client.post(
        "/api/admin/exclusion-policy/publications",
        headers={**headers, "X-Idempotency-Key": "policy-diagnostics-journey-001"},
        json={
            "policy_preview_id": str(record.policy_preview_id),
            "policy_draft_id": str(record.policy_draft_id),
            "expected_draft_version": record.draft_version,
            "expected_draft_sha256": record.draft_sha256,
            "preview_impact_digest": str(record.impact_digest),
            "expected_active_policy_revision_id": None,
            "expected_active_revision_number": 0,
            "confirmation": "PUBLISH EXCLUSION POLICY",
        },
    )
    assert published.status_code == 201, published.text


def _force_ready_preview(response: Any, policy_state: OfflineExclusionPolicyState) -> Any:
    preview_id = UUID(str(response.json()["data"]["policy_preview_id"]))
    record = policy_state.preview_rows[preview_id]
    ready = replace(
        record,
        status=PreviewStatus.READY,
        impact_digest=compute_impact_digest(()),
        ready_at=record.created_at,
        expires_at=record.created_at + timedelta(seconds=PREVIEW_READY_EXPIRY_SECONDS),
    )
    policy_state.preview_rows[preview_id] = ready
    return ready


# --- route membership and document shape ------------------------------------------------


def test_document_carries_exactly_the_one_get_operation(application: FastAPI) -> None:
    document = application.openapi()
    operations = document["paths"][_ROUTE_PATH]
    assert set(operations) == {"get"}
    assert operations["get"]["operationId"] == "getExclusionPolicyDiagnostics"
    schema = operations["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/ApiEnvelope_ExclusionPolicyDiagnosticsData_"}
    assert "security" not in operations["get"]


def test_diagnostics_payload_schemas_close_their_members(application: FastAPI) -> None:
    schemas = application.openapi()["components"]["schemas"]
    evaluation_counter = schemas["PolicyEvaluationCounterData"]
    assert set(evaluation_counter["properties"]) == {"boundary", "decision", "count"}
    assert evaluation_counter["properties"]["boundary"] == {
        "$ref": "#/components/schemas/PolicyBoundary"
    }
    assert evaluation_counter["properties"]["decision"] == {
        "$ref": "#/components/schemas/EvaluationMetricOutcome"
    }
    assert schemas["EvaluationMetricOutcome"]["enum"] == [
        "allowed",
        "excluded",
        "indeterminate",
        "failed",
    ]
    publication_counter = schemas["PolicyPublicationCounterData"]
    assert set(publication_counter["properties"]) == {"outcome", "count"}
    assert schemas["PublicationMetricOutcome"]["enum"] == [
        "published",
        "replayed",
        "rejected",
    ]
    failure_record = schemas["PolicyFailureRecordData"]
    assert set(failure_record["properties"]) == {"boundary", "error_code", "at_epoch_ms"}
    assert failure_record["properties"]["error_code"] == {"$ref": "#/components/schemas/ErrorCode"}
    assert failure_record["properties"]["at_epoch_ms"]["type"] == "integer"
    assert failure_record["properties"]["at_epoch_ms"]["minimum"] == 0
    diagnostics = schemas["ExclusionPolicyDiagnosticsData"]
    assert set(diagnostics["properties"]) == {
        "evaluation_counters",
        "publication_counters",
        "recent_failures",
    }


# --- authentication gates ----------------------------------------------------------------


def test_diagnostics_route_requires_an_authenticated_web_session(client: TestClient) -> None:
    anonymous = client.get(_ROUTE_PATH)
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "authentication_required"
    assert anonymous.headers["cache-control"] == "no-store"

    wrong_origin = client.get(_ROUTE_PATH, headers={"Origin": "https://attacker.example"})
    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["error"]["code"] == "csrf_validation_failed"


def test_diagnostics_route_rejects_device_credentials(client: TestClient) -> None:
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
    headers = _admin_session_headers(client)
    approved = client.post(
        f"/api/auth/device-authorizations/{grant['grant_id']}/approve", headers=headers
    )
    assert approved.status_code == 200, approved.text
    exchanged = client.post(
        f"/api/auth/device-authorizations/{grant['grant_id']}/poll",
        headers={"Authorization": f"Bearer {grant['polling_secret']}"},
    )
    assert exchanged.status_code == 200, exchanged.text
    access_credential = str(dict(exchanged.json()["data"])["access_credential"])
    client.cookies.clear()

    response = client.get(_ROUTE_PATH, headers={"Authorization": f"Bearer {access_credential}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


# --- closed payload ----------------------------------------------------------------------


def test_diagnostics_route_returns_only_closed_tokens_and_counts(
    client: TestClient, recorder: InMemoryExclusionPolicyMetrics
) -> None:
    _seed_closed_evidence(recorder)
    headers = _admin_session_headers(client)

    response = client.get(_ROUTE_PATH, headers=headers)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()["data"]
    assert set(payload) == {"evaluation_counters", "publication_counters", "recent_failures"}
    assert payload["evaluation_counters"] == [
        {
            "boundary": "single_part_upload",
            "decision": "allowed",
            "count": 1,
        },
        {
            "boundary": "single_part_upload",
            "decision": "failed",
            "count": 1,
        },
    ]
    assert payload["publication_counters"] == [{"outcome": "rejected", "count": 1}]
    (record,) = payload["recent_failures"]
    assert set(record) == {"boundary", "error_code", "at_epoch_ms"}
    assert record["boundary"] == "single_part_upload"
    assert record["error_code"] == "exclusion_policy_signing_unavailable"
    assert record["at_epoch_ms"] == _FIRST_EPOCH_MS

    rendered = str(payload)
    assert _REJECTED_OPERAND_SENTINEL not in rendered
    assert "duration" not in rendered


def test_diagnostics_counters_record_a_real_publication_journey(
    client: TestClient,
    recorder: InMemoryExclusionPolicyMetrics,
    policy_state: OfflineExclusionPolicyState,
) -> None:
    """The publication counters observe the real Admin publication route."""

    _publish_one_policy_revision(client, policy_state)
    headers = _admin_session_headers(client)

    response = client.get(_ROUTE_PATH, headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["publication_counters"] == [{"outcome": "published", "count": 1}]


def test_diagnostics_failure_ring_is_bounded_at_fifty_records(
    client: TestClient, recorder: InMemoryExclusionPolicyMetrics
) -> None:
    for _ in range(55):
        recorder.record_evaluation(
            boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
            decision=EvaluationMetricOutcome.FAILED,
            duration_seconds=0.0,
            error_code=ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED,
        )
    headers = _admin_session_headers(client)

    response = client.get(_ROUTE_PATH, headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["evaluation_counters"] == [
        {
            "boundary": "single_part_upload",
            "decision": "failed",
            "count": 55,
        }
    ]
    recent = payload["recent_failures"]
    assert len(recent) == 50
    timestamps = [record["at_epoch_ms"] for record in recent]
    assert timestamps == sorted(timestamps)
    assert timestamps[0] == _FIRST_EPOCH_MS + 5
    assert timestamps[-1] == _FIRST_EPOCH_MS + 54
