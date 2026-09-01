"""Closed policy metrics exposition route: membership and closed shape.

These tests compose the real application over the offline web-authentication
and exclusion-policy runtimes sharing one metrics recorder, then pin the one
authenticated read-only Prometheus text route of the policy metrics sink
(sink plan 2026-08-31): exact method and semantic operation id in the
document with exactly the one text/plain success entry, the strict Web
session gate (device credentials and anonymous calls close with the
registered authentication error and the no-store posture), and the rendered
exposition — only the closed evaluation counters by boundary and decision
(``failed`` included) and the closed publication outcome counters, never
the failure ring, a duration, a path, a locator or free-form text. A real
publication through the Admin routes proves the scrape renders the same
shared sink the diagnostics route reads, and a broken sink read answers the
typed retryable ``exclusion_policy_metrics_unavailable`` dependency error
instead of blocking anything.
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
    ExclusionPolicyDiagnostics,
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
_ROUTE_PATH: Final[str] = "/api/admin/metrics"
_TEXT_CONTENT_TYPE: Final[str] = "text/plain; version=0.0.4; charset=utf-8"
_REJECTED_OPERAND_SENTINEL: Final[str] = "/absolute/escape/path/do-not-emit"


class _ReadyProbe:
    """Readiness probe stub: the metrics route never consults it."""

    async def check(self) -> None: ...


class _FailingDiagnosticsSource:
    """Read-side double whose snapshot read fails like a broken sink.

    Raises an unexpected exception carrying an internal-detail sentinel that
    must never reach the response: the route maps the failure onto the typed
    closed dependency error instead.
    """

    def policy_diagnostics(self) -> ExclusionPolicyDiagnostics:
        raise RuntimeError("sink snapshot read exploded with /internal/detail")


@pytest.fixture
def recorder() -> InMemoryExclusionPolicyMetrics:
    return InMemoryExclusionPolicyMetrics()


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
        headers={**headers, "X-Idempotency-Key": "policy-metrics-journey-001"},
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
    assert operations["get"]["operationId"] == "getMetricsExposition"
    success = operations["get"]["responses"]["200"]
    assert set(success["content"]) == {_TEXT_CONTENT_TYPE}
    assert success["content"][_TEXT_CONTENT_TYPE]["schema"] == {"type": "string"}
    assert "security" not in operations["get"]


# --- authentication gates ----------------------------------------------------------------


def test_metrics_route_requires_an_authenticated_web_session(client: TestClient) -> None:
    anonymous = client.get(_ROUTE_PATH)
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "authentication_required"
    assert anonymous.headers["cache-control"] == "no-store"

    wrong_origin = client.get(_ROUTE_PATH, headers={"Origin": "https://attacker.example"})
    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["error"]["code"] == "csrf_validation_failed"


def test_metrics_route_rejects_device_credentials(client: TestClient) -> None:
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


# --- closed exposition -------------------------------------------------------------------


def test_metrics_route_renders_only_closed_tokens_and_counts(
    client: TestClient, recorder: InMemoryExclusionPolicyMetrics
) -> None:
    _seed_closed_evidence(recorder)
    headers = _admin_session_headers(client)

    response = client.get(_ROUTE_PATH, headers=headers)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == _TEXT_CONTENT_TYPE
    assert response.headers["cache-control"] == "no-store"
    assert response.text == (
        "# TYPE exclusion_policy_evaluation_total counter\n"
        'exclusion_policy_evaluation_total{boundary="single_part_upload",decision="allowed"} 1\n'
        'exclusion_policy_evaluation_total{boundary="single_part_upload",decision="failed"} 1\n'
        "# TYPE exclusion_policy_publication_total counter\n"
        'exclusion_policy_publication_total{outcome="rejected"} 1\n'
    )

    # Forbidden-substrate scan over the rendered output: closed tokens and
    # counts only — the failure ring (codes, epoch timestamps), durations,
    # ids, paths and free text never render. Every sample line is exactly
    # one of the two counter families over the closed label vocabularies
    # with a bare integer count.
    assert _REJECTED_OPERAND_SENTINEL not in response.text
    assert "duration" not in response.text
    assert "at_epoch" not in response.text
    assert "error_code" not in response.text
    closed_boundaries = {boundary.value for boundary in PolicyBoundary}
    closed_decisions = {decision.value for decision in EvaluationMetricOutcome}
    closed_outcomes = {outcome.value for outcome in PublicationMetricOutcome}
    for line in response.text.strip().splitlines():
        if line.startswith("# TYPE "):
            assert line in (
                "# TYPE exclusion_policy_evaluation_total counter",
                "# TYPE exclusion_policy_publication_total counter",
            )
            continue
        sample, count_text = line.rsplit(" ", 1)
        assert count_text.isdigit()
        if sample.startswith("exclusion_policy_evaluation_total{"):
            label_text = sample[len("exclusion_policy_evaluation_total{") : -1]
            labels = dict(part.split('="', 1) for part in label_text.split('",'))
            labels = {name: value.rstrip('"') for name, value in labels.items()}
            assert set(labels) == {"boundary", "decision"}
            assert labels["boundary"] in closed_boundaries
            assert labels["decision"] in closed_decisions
        else:
            assert sample.startswith("exclusion_policy_publication_total{")
            label_text = sample[len("exclusion_policy_publication_total{") : -1]
            ((name, value),) = [part.split('="', 1) for part in label_text.split('",')]
            assert name == "outcome"
            assert value.rstrip('"') in closed_outcomes


def test_metrics_exposition_renders_a_fresh_sink_as_the_two_type_headers(
    client: TestClient,
) -> None:
    """A fresh or fallback sink scrapes as a valid exposition with no samples."""

    response = client.get(_ROUTE_PATH, headers=_admin_session_headers(client))

    assert response.status_code == 200, response.text
    assert response.text == (
        "# TYPE exclusion_policy_evaluation_total counter\n"
        "# TYPE exclusion_policy_publication_total counter\n"
    )


def test_metrics_exposition_renders_the_real_publication_journey(
    client: TestClient,
    recorder: InMemoryExclusionPolicyMetrics,
    policy_state: OfflineExclusionPolicyState,
) -> None:
    """The scrape renders the same shared sink the publication journey records."""

    _publish_one_policy_revision(client, policy_state)

    response = client.get(_ROUTE_PATH, headers=_admin_session_headers(client))

    assert response.status_code == 200, response.text
    assert response.text == (
        "# TYPE exclusion_policy_evaluation_total counter\n"
        "# TYPE exclusion_policy_publication_total counter\n"
        'exclusion_policy_publication_total{outcome="published"} 1\n'
    )


# --- sink failure never blocks ------------------------------------------------------------


def test_metrics_sink_render_failure_answers_the_typed_dependency_error(
    policy_state: OfflineExclusionPolicyState,
) -> None:
    """A broken sink read closes the scrape with the closed dependency error.

    The sink is read-side only: evaluation keeps recording regardless, and
    the route answers the typed retryable ``exclusion_policy_metrics_unavailable``
    family — never a bare internal error and never the failing read's detail.
    """

    runtime = compose_offline_exclusion_policy(state=policy_state)
    failing_application = create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(
            clock=OfflineAuthenticationClock(),
            state=OfflineAuthenticationState(totp_active=False),
        ),
        exclusion_policy=replace(runtime, metrics_diagnostics=_FailingDiagnosticsSource()),
    )
    with TestClient(failing_application, base_url=ORIGIN) as failing_client:
        response = failing_client.get(_ROUTE_PATH, headers=_admin_session_headers(failing_client))

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "exclusion_policy_metrics_unavailable"
    assert payload["error"]["retryable"] is True
    assert response.headers["cache-control"] == "no-store"
    assert "/internal/detail" not in response.text
    assert "exploded" not in response.text
