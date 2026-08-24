"""Closed sync rejection diagnostics admin route: membership and closed shape.

These tests compose the real application over the offline web-authentication
and small-file-sync runtimes sharing one metrics recorder, then pin the one
authenticated read-only Admin route of the sync observability contract: exact
method and semantic operation id in the document, the strict Web session gate
(device credentials and anonymous calls close with the registered
authentication error and the no-store posture), and the closed payload —
rejection counters and a bounded ring of the last fifty rejection records
carrying only the closed error code, the epoch-millisecond timestamp and the
closed operation label. No path, locator, device id, digest or free-form
string can appear in the payload: the seeded rejections flow through the real
plugin preflight route first, and the route answers with closed tokens only.
"""

from __future__ import annotations

from collections.abc import Iterator
from hashlib import sha256
from typing import Any, Final
from uuid import uuid4

import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import (
    OFFLINE_WEB_ALLOWED_ORIGIN,
    OfflineAuthenticationClock,
    OfflineAuthenticationState,
    compose_offline_web_authentication,
)
from api_runtime.authentication_dependencies import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from api_runtime.small_file_sync_composition import compose_offline_small_file_sync
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.small_file_sync.metrics import InMemorySmallFileSyncMetrics

ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN
_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}
_FIRST_DIGEST: Final[str] = sha256(b"first seeded body").hexdigest()
_SECOND_DIGEST: Final[str] = sha256(b"second seeded body").hexdigest()
_LOCATOR: Final[str] = "notes/do-not-emit-seeded-locator.md"
_ROUTE_PATH: Final[str] = "/api/admin/sync/rejections"


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


def _preflight_body(sha256_text: str) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "operation": "create",
        "local_file_id": str(uuid4()),
        "source_id": None,
        "base_version_id": None,
        "normalized_locator": _LOCATOR,
        "sha256": sha256_text,
        "size_bytes": 32,
        "media_type": "text/markdown",
        "policy_revision": 7,
    }


@pytest.fixture
def recorder() -> InMemorySmallFileSyncMetrics:
    return InMemorySmallFileSyncMetrics(epoch_ms_clock=_SteppingEpochClock())


@pytest.fixture
def application(recorder: InMemorySmallFileSyncMetrics) -> FastAPI:
    return create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(
            clock=OfflineAuthenticationClock(),
            state=OfflineAuthenticationState(totp_active=False),
        ),
        small_file_sync=compose_offline_small_file_sync(metrics=recorder),
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


def _seed_one_rejection(client: TestClient, access_credential: str) -> None:
    """Drive exactly one identity-mismatch rejection through the plugin route."""

    reserved = _preflight_body(_FIRST_DIGEST)
    mismatched = dict(reserved)
    mismatched["sha256"] = _SECOND_DIGEST
    bearer = {"Authorization": f"Bearer {access_credential}"}
    first = client.post("/api/sync/journal-events/preflight", headers=bearer, json=reserved)
    assert first.status_code == 200, first.text
    second = client.post("/api/sync/journal-events/preflight", headers=bearer, json=mismatched)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "small_file_operation_identity_mismatch"


# --- route membership and document shape ------------------------------------------------


def test_document_carries_exactly_the_one_get_operation(application: FastAPI) -> None:
    document = application.openapi()
    operations = document["paths"][_ROUTE_PATH]
    assert set(operations) == {"get"}
    assert operations["get"]["operationId"] == "getSyncRejectionDiagnostics"
    schema = operations["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/ApiEnvelope_SmallFileRejectionDiagnosticsData_"}
    assert "security" not in operations["get"]


def test_diagnostics_payload_schemas_close_their_members(application: FastAPI) -> None:
    schemas = application.openapi()["components"]["schemas"]
    record = schemas["SmallFileRejectionRecordData"]
    assert set(record["properties"]) == {"error_code", "at_epoch_ms", "operation"}
    assert record["properties"]["error_code"] == {
        "$ref": "#/components/schemas/SmallFileRejectionReason"
    }
    assert record["properties"]["operation"] == {"$ref": "#/components/schemas/SmallFileOperation"}
    assert record["properties"]["at_epoch_ms"]["type"] == "integer"
    assert record["properties"]["at_epoch_ms"]["minimum"] == 0
    assert schemas["SmallFileRejectionReason"]["enum"] == [
        "small_file_preflight_invalid",
        "small_file_operation_not_found",
        "small_file_operation_expired",
        "small_file_operation_identity_mismatch",
        "small_file_size_limit_exceeded",
        "small_file_content_integrity_failed",
        "small_file_upload_state_invalid",
        # The policy-failure codes the preflight boundaries record into the
        # ring (policy-observability remediation C1): the two denial codes
        # keep the excluded outcome, the two system codes propagate as the
        # typed 409/503 errors — all four are ring-recordable codes.
        "exclusion_policy_denied",
        "exclusion_policy_indeterminate",
        "exclusion_policy_not_initialized",
        "exclusion_policy_signing_unavailable",
    ]
    assert schemas["SmallFileOperation"]["enum"] == ["create", "update"]
    counter = schemas["SmallFileRejectionCounterData"]
    assert set(counter["properties"]) == {"operation", "error_code", "count"}
    diagnostics = schemas["SmallFileRejectionDiagnosticsData"]
    assert set(diagnostics["properties"]) == {"rejection_counters", "recent_rejections"}


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
    client: TestClient, recorder: InMemorySmallFileSyncMetrics
) -> None:
    access_credential = _exchange_device_credential(client)
    _seed_one_rejection(client, access_credential)
    headers = _admin_session_headers(client)

    response = client.get(_ROUTE_PATH, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()["data"]
    assert set(payload) == {"rejection_counters", "recent_rejections"}
    assert payload["rejection_counters"] == [
        {
            "operation": "create",
            "error_code": "small_file_operation_identity_mismatch",
            "count": 1,
        }
    ]
    (record,) = payload["recent_rejections"]
    assert set(record) == {"error_code", "at_epoch_ms", "operation"}
    assert record["error_code"] == "small_file_operation_identity_mismatch"
    assert record["operation"] == "create"
    assert record["at_epoch_ms"] == 1_800_000_000_000

    rendered = str(payload)
    for forbidden in (_LOCATOR, _FIRST_DIGEST, _SECOND_DIGEST, access_credential):
        assert forbidden not in rendered


def test_diagnostics_ring_is_bounded_at_fifty_records(
    client: TestClient, recorder: InMemorySmallFileSyncMetrics
) -> None:
    access_credential = _exchange_device_credential(client)
    for _ in range(55):
        _seed_one_rejection(client, access_credential)
    headers = _admin_session_headers(client)

    response = client.get(_ROUTE_PATH, headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["rejection_counters"] == [
        {
            "operation": "create",
            "error_code": "small_file_operation_identity_mismatch",
            "count": 55,
        }
    ]
    recent = payload["recent_rejections"]
    assert len(recent) == 50
    timestamps = [record["at_epoch_ms"] for record in recent]
    assert timestamps == sorted(timestamps)
    assert timestamps[0] == 1_800_000_000_005
    assert timestamps[-1] == 1_800_000_000_054
