"""Closed source lifecycle diagnostics admin route: membership and closed shape.

These tests compose the real application over the offline web-authentication
and source-lifecycle runtimes sharing one metrics recorder, then pin the one
authenticated read-only Admin route of the lifecycle observability contract
(spec C3 of the closed-reason surfacing remediation): exact method and
semantic operation id in the document, the strict Web session gate (device
credentials and anonymous calls close with the registered authentication
error and the no-store posture), and the closed payload — commit counters
keyed by the closed operation and outcome labels with their counts, plus the
bounded ring of recent rejection records carrying only the closed error
code, the epoch-millisecond timestamp and the closed operation label. No
path, locator, device id, digest or free-form string can appear in the
payload: the replay counter flows through the real plugin lifecycle route
first, and the route answers with closed tokens only.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final
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
from api_runtime.source_lifecycle_composition import compose_offline_source_lifecycle
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.source_lifecycle.commands import LifecycleOperation
from personal_os.source_lifecycle.errors import SourceLifecycleErrorCode
from personal_os.source_lifecycle.metrics import InMemorySourceLifecycleMetrics

ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN
_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}
_ROUTE_PATH: Final[str] = "/api/admin/source-lifecycle/rejections"
_SOURCE_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000020")
_EVENT_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000021")
_EXPECTED_VERSION_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000022")


class _SteppingEpochClock:
    """Deterministic epoch-ms seam: every call advances by one millisecond."""

    def __init__(self, first_epoch_ms: int = 1_800_000_000_000) -> None:
        self._next_epoch_ms = first_epoch_ms

    def __call__(self) -> int:
        current = self._next_epoch_ms
        self._next_epoch_ms += 1
        return current


class _ReadyProbe:
    """Readiness probe stub: the diagnostics route never consults it."""

    async def check(self) -> None: ...


def _renew_body() -> dict[str, object]:
    """One valid rename event body: replaying it records the closed outcome."""

    return {
        "event_id": str(_EVENT_ID),
        "idempotency_key": f"lifecycle-diagnostics-{uuid4().hex}",
        "source_id": str(_SOURCE_ID),
        "operation": "rename",
        "expected_version_id": str(_EXPECTED_VERSION_ID),
        "expected_locator": "notes/do-not-emit-locator.md",
        "target_locator": "notes/do-not-emit-renamed.md",
        "tombstone_id": None,
        "policy_revision": 1,
        "client_timestamp": None,
    }


@pytest.fixture
def recorder() -> InMemorySourceLifecycleMetrics:
    return InMemorySourceLifecycleMetrics(epoch_ms_clock=_SteppingEpochClock())


@pytest.fixture
def application(recorder: InMemorySourceLifecycleMetrics) -> FastAPI:
    return create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(
            clock=OfflineAuthenticationClock(),
            state=OfflineAuthenticationState(totp_active=False),
        ),
        source_lifecycle=compose_offline_source_lifecycle(metrics=recorder),
    )


@pytest.fixture
def client(application: FastAPI) -> Iterator[TestClient]:
    with TestClient(application, base_url=ORIGIN) as test_client:
        yield test_client


def _exchange_device_credential(client: TestClient) -> str:
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
    return str(dict(exchanged.json()["data"])["access_credential"])


def _admin_session_headers(client: TestClient) -> dict[str, str]:
    login = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    assert login.status_code == 200, login.text
    return {
        "Origin": ORIGIN,
        "Cookie": (
            f"{SESSION_COOKIE_NAME}={login.cookies[SESSION_COOKIE_NAME]}; "
            f"{CSRF_COOKIE_NAME}={login.cookies[CSRF_COOKIE_NAME]}"
        ),
    }


# --- route membership and document shape ------------------------------------------------


def test_document_carries_exactly_the_one_get_operation(application: FastAPI) -> None:
    document = application.openapi()
    operations = document["paths"][_ROUTE_PATH]
    assert set(operations) == {"get"}
    assert operations["get"]["operationId"] == "getSourceLifecycleRejectionDiagnostics"
    schema = operations["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/ApiEnvelope_SourceLifecycleDiagnosticsData_"}
    assert "security" not in operations["get"]


def test_diagnostics_payload_schemas_close_their_members(application: FastAPI) -> None:
    schemas = application.openapi()["components"]["schemas"]
    record = schemas["SourceLifecycleRejectionRecordData"]
    assert set(record["properties"]) == {"error_code", "at_epoch_ms", "operation"}
    assert record["properties"]["error_code"] == {
        "$ref": "#/components/schemas/SourceLifecycleErrorCode"
    }
    assert record["properties"]["operation"] == {"$ref": "#/components/schemas/LifecycleOperation"}
    assert record["properties"]["at_epoch_ms"]["type"] == "integer"
    assert record["properties"]["at_epoch_ms"]["minimum"] == 0
    assert schemas["SourceLifecycleErrorCode"]["enum"] == [
        "source_lifecycle_input_invalid",
        "source_locator_missing",
        "source_locator_conflict",
        "source_tombstone_not_found",
        "source_tombstone_closed",
        "source_lifecycle_version_conflict",
        "source_lifecycle_commit_outcome_unknown",
    ]
    assert schemas["LifecycleOperation"]["enum"] == ["rename", "move", "delete", "restore"]
    assert schemas["LifecycleMetricOutcome"]["enum"] == ["committed", "rejected", "replayed"]
    counter = schemas["SourceLifecycleCommitCounterData"]
    assert set(counter["properties"]) == {"operation", "outcome", "count"}
    diagnostics = schemas["SourceLifecycleDiagnosticsData"]
    assert set(diagnostics["properties"]) == {"commit_counters", "recent_rejections"}


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
    access_credential = _exchange_device_credential(client)
    client.cookies.clear()
    response = client.get(_ROUTE_PATH, headers={"Authorization": f"Bearer {access_credential}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


# --- closed payload ----------------------------------------------------------------------


def test_diagnostics_route_returns_only_closed_tokens_and_counts(
    client: TestClient, recorder: InMemorySourceLifecycleMetrics
) -> None:
    access_credential = _exchange_device_credential(client)
    body = _renew_body()
    bearer = {"Authorization": f"Bearer {access_credential}"}
    first = client.post("/api/sources/lifecycle-events", headers=bearer, json=body)
    assert first.status_code == 200, first.text
    replay = client.post("/api/sources/lifecycle-events", headers=bearer, json=body)
    assert replay.status_code == 200, replay.text
    recorder.record_rejection(
        operation=LifecycleOperation.RENAME,
        error_code=SourceLifecycleErrorCode.LOCATOR_CONFLICT,
    )
    headers = _admin_session_headers(client)

    response = client.get(_ROUTE_PATH, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()["data"]
    assert set(payload) == {"commit_counters", "recent_rejections"}
    assert payload["commit_counters"] == [
        {"operation": "rename", "outcome": "replayed", "count": 1}
    ]
    (record,) = payload["recent_rejections"]
    assert set(record) == {"error_code", "at_epoch_ms", "operation"}
    assert record["error_code"] == "source_locator_conflict"
    assert record["operation"] == "rename"
    assert record["at_epoch_ms"] == 1_800_000_000_000

    rendered = str(payload)
    for forbidden in ("do-not-emit-locator", "do-not-emit-renamed", access_credential):
        assert forbidden not in rendered


def test_diagnostics_ring_is_bounded_at_fifty_records(
    client: TestClient, recorder: InMemorySourceLifecycleMetrics
) -> None:
    for _ in range(55):
        recorder.record_rejection(
            operation=LifecycleOperation.RENAME,
            error_code=SourceLifecycleErrorCode.LOCATOR_CONFLICT,
        )
    headers = _admin_session_headers(client)

    response = client.get(_ROUTE_PATH, headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["commit_counters"] == []
    recent = payload["recent_rejections"]
    assert len(recent) == 50
    timestamps = [record["at_epoch_ms"] for record in recent]
    assert timestamps == sorted(timestamps)
    assert timestamps[0] == 1_800_000_000_005
    assert timestamps[-1] == 1_800_000_000_054
