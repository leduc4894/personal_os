"""Exclusion-policy Admin and plugin HTTP routes over the offline compositions.

These tests drive the seven closed policy routes of spec 16 through the real
application factory wired with the offline deterministic web-authentication
and exclusion-policy compositions: no database, no key file and no
environment read. The Admin surface answers only behind the exact-origin
session/CSRF contract — publication additionally behind the recent
re-authentication window — and derives workspace and actor from the resolved
session; the plugin surface accepts exactly the ``obsidian_sync`` access
Bearer credential and derives workspace and device from it. Every response
carries ``Cache-Control: no-store``; preview reads answer 202 while
pending/running and 200 once ready; publication answers 201 once and 200 for
the exact replay; the keyset page is bounded and ordered; the snapshot ETag
is the quoted payload SHA-256 and ``304`` happens only after the caller
authenticated.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4, uuid5

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
from api_runtime.exclusion_policy_composition import (
    KEYSET_PAGE_MAXIMUM,
    OfflineExclusionPolicyState,
    compose_offline_exclusion_policy,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    PreviewMatchState,
    RawPolicyDecision,
    RuleKind,
)
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.ports import PolicyKeysetRecord
from personal_os.exclusion_policy.previews import (
    PREVIEW_READY_EXPIRY_SECONDS,
    PREVIEW_RESULT_PAGE_MAXIMUM,
    PolicyPreviewRecord,
    PolicyPreviewResultRow,
    PreviewImpactClass,
    PreviewStatus,
    compute_impact_digest,
)
from personal_os.exclusion_policy.signatures import (
    build_keyset_payload,
    compute_payload_sha256_hex,
)
from personal_os.runtime_configuration.models import RuntimeEnvironment

ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN
_RULE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-0000000000a1")
_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}
_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


class _ReadyProbe:
    """Readiness probe stub: the policy routes never consult dependencies."""

    async def check(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PolicyRouteHarness:
    """One test client bound to the shared offline state."""

    client: TestClient
    clock: OfflineAuthenticationClock
    auth_state: OfflineAuthenticationState
    policy_state: OfflineExclusionPolicyState


@pytest.fixture
def harness() -> Iterator[PolicyRouteHarness]:
    clock = OfflineAuthenticationClock()
    auth_state = OfflineAuthenticationState(totp_active=False)
    policy_state = OfflineExclusionPolicyState()
    application: FastAPI = create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(clock=clock, state=auth_state),
        exclusion_policy=compose_offline_exclusion_policy(state=policy_state),
    )
    with TestClient(application, base_url=ORIGIN) as test_client:
        yield PolicyRouteHarness(
            client=test_client, clock=clock, auth_state=auth_state, policy_state=policy_state
        )


def login(test_client: TestClient) -> dict[str, str]:
    response = test_client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    assert response.status_code == 200, response.text
    return {
        "session": response.cookies[SESSION_COOKIE_NAME],
        "csrf": response.cookies[CSRF_COOKIE_NAME],
    }


def session_headers(cookies: dict[str, str], *, csrf: bool = True) -> dict[str, str]:
    headers = {
        "Origin": ORIGIN,
        "Cookie": (
            f"{SESSION_COOKIE_NAME}={cookies['session']}; {CSRF_COOKIE_NAME}={cookies['csrf']}"
        ),
    }
    if csrf:
        headers["X-CSRF-Token"] = cookies["csrf"]
    return headers


def exchange_access_credential(harness: PolicyRouteHarness) -> str:
    """Exchange one device grant through the real routes; return the at1."""
    created = harness.client.post(
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
    cookies = login(harness.client)
    approved = harness.client.post(
        f"/api/auth/device-authorizations/{grant['grant_id']}/approve",
        headers=session_headers(cookies),
    )
    assert approved.status_code == 200, approved.text
    exchanged = harness.client.post(
        f"/api/auth/device-authorizations/{grant['grant_id']}/poll",
        headers={"Authorization": f"Bearer {grant['polling_secret']}"},
    )
    assert exchanged.status_code == 200, exchanged.text
    return str(exchanged.json()["data"]["access_credential"])


def put_draft(
    harness: PolicyRouteHarness,
    cookies: dict[str, str],
    rules: list[dict[str, Any]],
    expected_version: int,
) -> Any:
    return harness.client.put(
        "/api/admin/exclusion-policy/draft",
        headers=session_headers(cookies),
        json={"expected_draft_version": expected_version, "rules": rules},
    )


def folder_rule(rule_id: UUID, prefix: str) -> dict[str, Any]:
    return {"rule_id": str(rule_id), "rule_kind": "folder_prefix", "folder_prefix": prefix}


def request_preview(harness: PolicyRouteHarness, cookies: dict[str, str]) -> Any:
    return harness.client.post(
        "/api/admin/exclusion-policy/previews", headers=session_headers(cookies)
    )


def seed_ready_preview(harness: PolicyRouteHarness) -> PolicyPreviewRecord:
    """Drive one preview to the ready state in the offline store."""
    cookies = login(harness.client)
    put_draft(
        harness,
        cookies,
        [folder_rule(_RULE_ID, "notes/private")],
        expected_version=1,
    )
    created = request_preview(harness, cookies)
    assert created.status_code == 202, created.text
    preview_id = UUID(str(created.json()["data"]["policy_preview_id"]))
    record = harness.policy_state.preview_rows[preview_id]
    ready = replace(
        record,
        status=PreviewStatus.READY,
        impact_digest=compute_impact_digest(()),
        ready_at=_FIXED_NOW,
        expires_at=_FIXED_NOW + timedelta(seconds=PREVIEW_READY_EXPIRY_SECONDS),
    )
    harness.policy_state.preview_rows[preview_id] = ready
    return ready


def publish_body(preview: PolicyPreviewRecord) -> dict[str, Any]:
    assert preview.impact_digest is not None
    return {
        "policy_preview_id": str(preview.policy_preview_id),
        "policy_draft_id": str(preview.policy_draft_id),
        "expected_draft_version": preview.draft_version,
        "expected_draft_sha256": preview.draft_sha256,
        "preview_impact_digest": preview.impact_digest,
        "expected_active_policy_revision_id": (
            None
            if preview.base_policy_revision_id is None
            else str(preview.base_policy_revision_id)
        ),
        "expected_active_revision_number": 0 if preview.base_policy_revision_id is None else 1,
        "confirmation": "PUBLISH EXCLUSION POLICY",
    }


def publish(
    harness: PolicyRouteHarness,
    cookies: dict[str, str],
    preview: PolicyPreviewRecord,
    idempotency_key: str = "publish-once-001",
) -> Any:
    return harness.client.post(
        "/api/admin/exclusion-policy/publications",
        headers={
            **session_headers(cookies),
            "X-Idempotency-Key": idempotency_key,
        },
        json=publish_body(preview),
    )


# --- Admin authorization gates ------------------------------------------------------------


def test_admin_routes_require_the_web_session(harness: PolicyRouteHarness) -> None:
    response = harness.client.get("/api/admin/exclusion-policy", headers={"Origin": ORIGIN})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["cache-control"] == "no-store"


def test_admin_routes_require_the_exact_origin(harness: PolicyRouteHarness) -> None:
    cookies = login(harness.client)
    response = harness.client.get(
        "/api/admin/exclusion-policy",
        headers={
            "Origin": "https://evil.example",
            "Cookie": f"{SESSION_COOKIE_NAME}={cookies['session']}",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"


def test_plugin_routes_accept_only_the_access_bearer(harness: PolicyRouteHarness) -> None:
    missing = harness.client.get("/api/sync/exclusion-policy/keysets")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "device_credential_invalid"

    cookies = login(harness.client)
    session_only = harness.client.get(
        "/api/sync/exclusion-policy/keysets", headers=session_headers(cookies)
    )
    assert session_only.status_code == 401
    assert session_only.json()["error"]["code"] == "device_credential_invalid"

    unknown = harness.client.get(
        "/api/sync/exclusion-policy/keysets",
        headers={"Authorization": f"Bearer at1.{uuid4()}.{bytes(range(32)).hex()}"},
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"]["code"] == "device_credential_invalid"
    assert unknown.headers["cache-control"] == "no-store"


# --- status and draft ---------------------------------------------------------------------


def test_status_returns_revision_metadata_draft_and_reconciliation(
    harness: PolicyRouteHarness,
) -> None:
    cookies = login(harness.client)
    response = harness.client.get(
        "/api/admin/exclusion-policy", headers=session_headers(cookies, csrf=False)
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["active_policy_revision_id"] is None
    assert data["active_revision_number"] == 0
    assert data["draft"]["draft_version"] == 1
    assert data["draft"]["rules"] == []
    assert data["reconciliation"] is None


def test_draft_replacement_requires_csrf(harness: PolicyRouteHarness) -> None:
    cookies = login(harness.client)
    response = harness.client.put(
        "/api/admin/exclusion-policy/draft",
        headers=session_headers(cookies, csrf=False),
        json={"expected_draft_version": 1, "rules": []},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"


@pytest.mark.parametrize(
    "body",
    [
        {
            "expected_draft_version": 1,
            "rules": [],
            "workspace_id": "00000000-0000-7000-8000-000000000001",
        },
        {
            "expected_draft_version": 1,
            "rules": [],
            "actor_user_id": "00000000-0000-7000-8000-000000000002",
        },
        {"rules": []},
        {"expected_draft_version": 0, "rules": []},
        {"expected_draft_version": 1, "rules": [], "base_policy_revision_id": None},
    ],
)
def test_draft_replacement_rejects_non_contract_bodies(
    harness: PolicyRouteHarness, body: dict[str, Any]
) -> None:
    cookies = login(harness.client)
    response = harness.client.put(
        "/api/admin/exclusion-policy/draft", headers=session_headers(cookies), json=body
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"


def test_draft_replacement_normalizes_rules_and_increments_version(
    harness: PolicyRouteHarness,
) -> None:
    cookies = login(harness.client)
    response = put_draft(harness, cookies, [folder_rule(_RULE_ID, "notes/private")], 1)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["draft_version"] == 2
    assert len(data["rules"]) == 1
    rule = data["rules"][0]
    assert rule["rule_kind"] == "folder_prefix"
    assert rule["folder_prefix"] == "notes/private"
    assert len(rule["semantic_fingerprint"]) == 64
    expected_fingerprint = normalize_rule(
        _RULE_ID, RuleKind.FOLDER_PREFIX, text_operand="notes/private"
    ).semantic_fingerprint
    assert rule["semantic_fingerprint"] == expected_fingerprint


def test_draft_replacement_maps_typed_input_errors_without_echoing_values(
    harness: PolicyRouteHarness,
) -> None:
    cookies = login(harness.client)
    response = put_draft(harness, cookies, [folder_rule(_RULE_ID, "/absolute/escape")], 1)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "exclusion_policy_input_invalid"
    assert error["details"] == {"reason": "locator_absolute"}
    assert "/absolute/escape" not in response.text


def test_draft_replacement_conflict_carries_the_current_version(
    harness: PolicyRouteHarness,
) -> None:
    cookies = login(harness.client)
    stale = put_draft(harness, cookies, [], 7)
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "exclusion_policy_draft_conflict"
    assert error["details"] == {"current_draft_version": 1}


# --- previews ------------------------------------------------------------------------------


def test_preview_creation_returns_202_and_polls_pending(harness: PolicyRouteHarness) -> None:
    cookies = login(harness.client)
    created = request_preview(harness, cookies)
    assert created.status_code == 202, created.text
    assert created.headers["cache-control"] == "no-store"
    data = created.json()["data"]
    assert data["status"] == "pending"
    assert data["policy_draft_id"] == str(harness.policy_state.draft.draft_id)
    assert data["draft_version"] == 1

    preview_id = data["policy_preview_id"]
    polled = harness.client.get(
        f"/api/admin/exclusion-policy/previews/{preview_id}",
        headers=session_headers(cookies, csrf=False),
    )
    assert polled.status_code == 202
    assert polled.json()["data"]["status"] == "pending"
    assert polled.json()["data"]["results"] is None


def test_preview_read_ready_returns_200_with_the_bounded_page(
    harness: PolicyRouteHarness,
) -> None:
    ready = seed_ready_preview(harness)
    cookies = login(harness.client)
    response = harness.client.get(
        f"/api/admin/exclusion-policy/previews/{ready.policy_preview_id}",
        headers=session_headers(cookies, csrf=False),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "ready"
    assert data["impact_digest"] == compute_impact_digest(())
    assert data["source_checkpoint_event_sequence"] == 0
    assert data["results"] == []
    assert data["next_cursor"] is None


_RESULT_NAMESPACE: Final[UUID] = UUID("00000000-0000-7000-8000-00000000feed")
_RESULT_IMPACT_CLASSES: Final[tuple[PreviewImpactClass, ...]] = tuple(PreviewImpactClass)


def _seeded_result_row(index: int) -> PolicyPreviewResultRow:
    """One deterministic result row cycling every closed impact class."""

    return PolicyPreviewResultRow(
        source_id=uuid5(_RESULT_NAMESPACE, f"result:{index}"),
        previous_raw_decision=RawPolicyDecision.ALLOWED,
        previous_enforced_decision=EnforcedPolicyDecision.ALLOWED,
        proposed_raw_decision=RawPolicyDecision.EXCLUDED,
        proposed_enforced_decision=EnforcedPolicyDecision.EXCLUDED,
        proposed_match_state=PreviewMatchState.MATCHED,
        impact_class=_RESULT_IMPACT_CLASSES[index % len(_RESULT_IMPACT_CLASSES)],
        matched_rule_ids=(_RULE_ID,),
        missing_fields=(),
        subject_fingerprint="f" * 64,
    )


def _seed_result_rows(
    harness: PolicyRouteHarness, preview: PolicyPreviewRecord, count: int
) -> list[PolicyPreviewResultRow]:
    rows = [_seeded_result_row(index) for index in range(count)]
    harness.policy_state.preview_result_rows[preview.policy_preview_id] = rows
    return rows


def test_preview_ready_result_page_is_capped_and_continuable(
    harness: PolicyRouteHarness,
) -> None:
    ready = seed_ready_preview(harness)
    rows = _seed_result_rows(harness, ready, PREVIEW_RESULT_PAGE_MAXIMUM + 5)
    expected_order = sorted(rows, key=lambda row: (row.impact_class.value, str(row.source_id)))
    cookies = login(harness.client)

    first = harness.client.get(
        f"/api/admin/exclusion-policy/previews/{ready.policy_preview_id}",
        headers=session_headers(cookies, csrf=False),
    )
    assert first.status_code == 200, first.text
    page = first.json()["data"]
    # (a) the page is capped at the spec 10 bound, in stable cursor order.
    assert len(page["results"]) == PREVIEW_RESULT_PAGE_MAXIMUM
    assert [row["source_id"] for row in page["results"]] == [
        str(row.source_id) for row in expected_order[:PREVIEW_RESULT_PAGE_MAXIMUM]
    ]
    # (b) the continuation cursor echoes the last row of the page exactly.
    boundary = expected_order[PREVIEW_RESULT_PAGE_MAXIMUM - 1]
    assert page["next_cursor"] == {
        "impact_class": boundary.impact_class.value,
        "source_id": str(boundary.source_id),
    }

    # (c) the continuation serves the remaining rows in the same order.
    second = harness.client.get(
        f"/api/admin/exclusion-policy/previews/{ready.policy_preview_id}",
        headers=session_headers(cookies, csrf=False),
        params={
            "cursor_impact_class": boundary.impact_class.value,
            "cursor_source_id": str(boundary.source_id),
        },
    )
    assert second.status_code == 200, second.text
    tail = second.json()["data"]
    assert [row["source_id"] for row in tail["results"]] == [
        str(row.source_id) for row in expected_order[PREVIEW_RESULT_PAGE_MAXIMUM:]
    ]
    assert tail["next_cursor"] is None
    # The boundary row itself never repeats on the continuation.
    assert str(boundary.source_id) not in {row["source_id"] for row in tail["results"]}


def test_preview_read_rejects_incomplete_or_unknown_cursors(
    harness: PolicyRouteHarness,
) -> None:
    ready = seed_ready_preview(harness)
    _seed_result_rows(harness, ready, 3)
    cookies = login(harness.client)
    path = f"/api/admin/exclusion-policy/previews/{ready.policy_preview_id}"

    half_cursor = harness.client.get(
        path,
        headers=session_headers(cookies, csrf=False),
        params={"cursor_impact_class": "still_excluded"},
    )
    assert half_cursor.status_code == 422
    error = half_cursor.json()["error"]
    assert error["code"] == "exclusion_policy_input_invalid"
    assert error["details"] == {"reason": "preview_cursor_invalid"}

    unknown_class = harness.client.get(
        path,
        headers=session_headers(cookies, csrf=False),
        params={"cursor_impact_class": "not-a-class", "cursor_source_id": str(_RULE_ID)},
    )
    assert unknown_class.status_code == 422
    assert unknown_class.json()["error"]["details"] == {"reason": "preview_cursor_invalid"}
    assert "not-a-class" not in unknown_class.text

    # A complete, well-formed cursor still answers the ready page: a cursor
    # sorting before every seeded row serves the full remaining page.
    complete = harness.client.get(
        path,
        headers=session_headers(cookies, csrf=False),
        params={"cursor_impact_class": "indeterminate", "cursor_source_id": str(_RULE_ID)},
    )
    assert complete.status_code == 200, complete.text
    assert len(complete.json()["data"]["results"]) == 3


def test_preview_read_failed_and_expired_map_to_the_closed_envelope(
    harness: PolicyRouteHarness,
) -> None:
    cookies = login(harness.client)
    created = request_preview(harness, cookies)
    preview_id = UUID(str(created.json()["data"]["policy_preview_id"]))

    failed = replace(
        harness.policy_state.preview_rows[preview_id],
        status=PreviewStatus.FAILED,
        safe_error_code="preview_execution_failed",
    )
    harness.policy_state.preview_rows[preview_id] = failed
    response = harness.client.get(
        f"/api/admin/exclusion-policy/previews/{preview_id}",
        headers=session_headers(cookies, csrf=False),
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "exclusion_policy_preview_failed"
    assert error["details"] == {"reason": "preview_execution_failed"}

    expired = replace(failed, status=PreviewStatus.EXPIRED, safe_error_code=None)
    harness.policy_state.preview_rows[preview_id] = expired
    response = harness.client.get(
        f"/api/admin/exclusion-policy/previews/{preview_id}",
        headers=session_headers(cookies, csrf=False),
    )
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "exclusion_policy_preview_expired"


def test_preview_read_of_an_unknown_preview_is_the_failed_envelope(
    harness: PolicyRouteHarness,
) -> None:
    cookies = login(harness.client)
    response = harness.client.get(
        f"/api/admin/exclusion-policy/previews/{uuid4()}",
        headers=session_headers(cookies, csrf=False),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "exclusion_policy_preview_failed"


# --- publication ---------------------------------------------------------------------------


def test_publication_requires_recent_reauthentication(harness: PolicyRouteHarness) -> None:
    preview = seed_ready_preview(harness)
    cookies = login(harness.client)
    harness.clock.database_now_value += timedelta(minutes=30)
    response = publish(harness, cookies, preview)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "recent_authentication_required"
    assert response.headers["cache-control"] == "no-store"


def test_publication_commits_once_then_replays_exactly(harness: PolicyRouteHarness) -> None:
    preview = seed_ready_preview(harness)
    cookies = login(harness.client)
    committed = publish(harness, cookies, preview, idempotency_key="key-one")
    assert committed.status_code == 201, committed.text
    data = committed.json()["data"]
    assert data["revision_number"] == 1
    assert data["parent_policy_revision_id"] is None
    assert data["rule_count"] == 1
    assert data["is_replay"] is False
    assert data["reconciliation_status"] == "pending"
    assert len(data["payload_sha256"]) == 64

    replayed = publish(harness, cookies, preview, idempotency_key="key-one")
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["data"] == {**data, "is_replay": True}


def test_publication_rejects_the_wrong_confirmation_phrase(harness: PolicyRouteHarness) -> None:
    preview = seed_ready_preview(harness)
    cookies = login(harness.client)
    body = {**publish_body(preview), "confirmation": "publish exclusion policy"}
    response = harness.client.post(
        "/api/admin/exclusion-policy/publications",
        headers={**session_headers(cookies), "X-Idempotency-Key": "key-two"},
        json=body,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "exclusion_policy_confirmation_invalid"


def test_publication_requires_the_idempotency_header(harness: PolicyRouteHarness) -> None:
    preview = seed_ready_preview(harness)
    cookies = login(harness.client)
    response = harness.client.post(
        "/api/admin/exclusion-policy/publications",
        headers=session_headers(cookies),
        json=publish_body(preview),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"


def test_publication_of_a_pending_preview_is_the_retryable_conflict(
    harness: PolicyRouteHarness,
) -> None:
    cookies = login(harness.client)
    created = request_preview(harness, cookies)
    preview = harness.policy_state.preview_rows[
        UUID(str(created.json()["data"]["policy_preview_id"]))
    ]
    response = harness.client.post(
        "/api/admin/exclusion-policy/publications",
        headers={**session_headers(cookies), "X-Idempotency-Key": "key-pending"},
        json={
            "policy_preview_id": str(preview.policy_preview_id),
            "policy_draft_id": str(preview.policy_draft_id),
            "expected_draft_version": preview.draft_version,
            "expected_draft_sha256": preview.draft_sha256,
            "preview_impact_digest": "d" * 64,
            "expected_active_policy_revision_id": None,
            "expected_active_revision_number": 0,
            "confirmation": "PUBLISH EXCLUSION POLICY",
        },
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "exclusion_policy_preview_pending"
    assert error["retryable"] is True


# --- plugin keysets and snapshot -----------------------------------------------------------


def seed_keyset_revisions(harness: PolicyRouteHarness, count: int) -> None:
    """Append deterministic keyset revisions after revision 1."""
    for revision in range(2, count + 1):
        payload = build_keyset_payload(
            workspace_id=harness.policy_state.workspace_id,
            keyset_revision=revision,
            parent_keyset_revision=revision - 1,
            created_at=_FIXED_NOW,
            keys=(),
        )
        harness.policy_state.keyset_rows.append(
            PolicyKeysetRecord(
                policy_keyset_id=UUID(int=revision),
                workspace_id=harness.policy_state.workspace_id,
                keyset_revision=revision,
                parent_keyset_revision=revision - 1,
                canonical_payload_bytes=payload,
                payload_sha256=compute_payload_sha256_hex(payload),
                keys=(),
                signatures=(),
                created_by_user_id=None,
                created_at=_FIXED_NOW,
            )
        )


def test_keyset_page_is_ordered_and_bounded(harness: PolicyRouteHarness) -> None:
    seed_keyset_revisions(harness, KEYSET_PAGE_MAXIMUM + 4)
    credential = exchange_access_credential(harness)
    headers = {"Authorization": f"Bearer {credential}"}

    first = harness.client.get("/api/sync/exclusion-policy/keysets", headers=headers)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    page = first.json()["data"]
    assert len(page["keysets"]) == KEYSET_PAGE_MAXIMUM
    revisions = [envelope["payload"]["keyset_revision"] for envelope in page["keysets"]]
    assert revisions == list(range(1, KEYSET_PAGE_MAXIMUM + 1))
    assert page["has_more"] is True

    second = harness.client.get(
        "/api/sync/exclusion-policy/keysets",
        headers=headers,
        params={"after_keyset_revision": KEYSET_PAGE_MAXIMUM},
    )
    assert second.status_code == 200, second.text
    tail = second.json()["data"]
    assert [envelope["payload"]["keyset_revision"] for envelope in tail["keysets"]] == [
        KEYSET_PAGE_MAXIMUM + 1,
        KEYSET_PAGE_MAXIMUM + 2,
        KEYSET_PAGE_MAXIMUM + 3,
        KEYSET_PAGE_MAXIMUM + 4,
    ]
    assert tail["has_more"] is False

    negative = harness.client.get(
        "/api/sync/exclusion-policy/keysets",
        headers=headers,
        params={"after_keyset_revision": -1},
    )
    assert negative.status_code == 422


def test_keyset_revision_one_envelope_carries_the_signed_trust_anchor(
    harness: PolicyRouteHarness,
) -> None:
    credential = exchange_access_credential(harness)
    response = harness.client.get(
        "/api/sync/exclusion-policy/keysets",
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert response.status_code == 200, response.text
    envelope = response.json()["data"]["keysets"][0]
    assert envelope["payload"]["contract"] == "exclusion_policy_keyset/v1"
    assert envelope["payload"]["keyset_revision"] == 1
    assert envelope["payload"]["parent_keyset_revision"] is None
    key = envelope["payload"]["keys"][0]
    assert key["state"] == "current"
    assert key["key_id"].startswith("ed25519-sha256-")
    assert envelope["payload_sha256"] == compute_payload_sha256_hex(
        harness.policy_state.keyset_rows[0].canonical_payload_bytes
    )
    assert len(envelope["signatures"]) == 1
    assert envelope["signatures"][0]["algorithm"] == "Ed25519"


def test_keysets_before_initialization_map_to_not_initialized(
    harness: PolicyRouteHarness,
) -> None:
    harness.policy_state.keyset_rows.clear()
    credential = exchange_access_credential(harness)
    response = harness.client.get(
        "/api/sync/exclusion-policy/keysets",
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "exclusion_policy_not_initialized"


def test_snapshot_serves_the_signed_envelope_with_the_quoted_digest_etag(
    harness: PolicyRouteHarness,
) -> None:
    preview = seed_ready_preview(harness)
    cookies = login(harness.client)
    committed = publish(harness, cookies, preview)
    assert committed.status_code == 201, committed.text
    payload_sha256 = str(committed.json()["data"]["payload_sha256"])
    credential = exchange_access_credential(harness)

    response = harness.client.get(
        "/api/sync/exclusion-policy/snapshot",
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["etag"] == f'"{payload_sha256}"'
    data = response.json()["data"]
    assert data["payload"]["contract"] == "exclusion_policy_snapshot/v1"
    assert data["payload"]["revision_number"] == 1
    assert data["payload"]["default_decision"] == "allowed"
    assert data["payload_sha256"] == payload_sha256
    assert data["signature"]["algorithm"] == "Ed25519"
    assert data["signature"]["key_id"].startswith("ed25519-sha256-")

    # The conditional GET revalidates only after authentication succeeded.
    unauthenticated = harness.client.get(
        "/api/sync/exclusion-policy/snapshot",
        headers={"If-None-Match": f'"{payload_sha256}"'},
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "device_credential_invalid"

    not_modified = harness.client.get(
        "/api/sync/exclusion-policy/snapshot",
        headers={
            "Authorization": f"Bearer {credential}",
            "If-None-Match": f'"{payload_sha256}"',
        },
    )
    assert not_modified.status_code == 304
    assert not_modified.headers["etag"] == f'"{payload_sha256}"'
    assert not_modified.headers["cache-control"] == "no-store"
    assert not_modified.content == b""


def test_snapshot_before_publication_maps_to_not_initialized(
    harness: PolicyRouteHarness,
) -> None:
    credential = exchange_access_credential(harness)
    response = harness.client.get(
        "/api/sync/exclusion-policy/snapshot",
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "exclusion_policy_not_initialized"


def test_status_reflects_publication_and_reconciliation(harness: PolicyRouteHarness) -> None:
    preview = seed_ready_preview(harness)
    cookies = login(harness.client)
    committed = publish(harness, cookies, preview)
    assert committed.status_code == 201, committed.text
    revision_id = str(committed.json()["data"]["policy_revision_id"])

    status = harness.client.get(
        "/api/admin/exclusion-policy", headers=session_headers(cookies, csrf=False)
    )
    data = status.json()["data"]
    assert data["active_policy_revision_id"] == revision_id
    assert data["active_revision_number"] == 1
    assert data["reconciliation"]["policy_revision_id"] == revision_id
    assert data["reconciliation"]["state"] == "pending"
    assert data["draft"]["base_policy_revision_id"] == revision_id


def test_status_renders_the_reconciliation_safe_error_code(harness: PolicyRouteHarness) -> None:
    """The failed reconciliation's durable reason reaches the Admin summary.

    A pending summary renders the null-safe absent reason; a terminal row
    renders its closed ``safe_error_code`` token with parity to the preview
    surface.
    """

    preview = seed_ready_preview(harness)
    cookies = login(harness.client)
    committed = publish(harness, cookies, preview)
    assert committed.status_code == 201, committed.text
    revision_id = str(committed.json()["data"]["policy_revision_id"])

    pending = harness.client.get(
        "/api/admin/exclusion-policy", headers=session_headers(cookies, csrf=False)
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["data"]["reconciliation"]["safe_error_code"] is None

    summary = harness.policy_state.reconciliation_summary
    assert summary is not None
    harness.policy_state.reconciliation_summary = replace(
        summary, state="terminal", safe_error_code="reconciliation_dispatch_terminal"
    )
    failed = harness.client.get(
        "/api/admin/exclusion-policy", headers=session_headers(cookies, csrf=False)
    )
    assert failed.status_code == 200, failed.text
    rendered = failed.json()["data"]["reconciliation"]
    assert rendered["policy_revision_id"] == revision_id
    assert rendered["state"] == "terminal"
    assert rendered["safe_error_code"] == "reconciliation_dispatch_terminal"
