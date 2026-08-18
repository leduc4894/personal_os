"""Exclusion-policy sentinel leak scan across the emitted HTTP surfaces.

One journey drives the real composed application over both offline
compositions through the policy-relevant exchanges: a rejected draft rule
whose operand must never echo, an accepted draft that legitimately renders
its operand, a preview lifecycle, one publication, and the plugin keyset and
snapshot reads. Every exchanged byte is captured and scanned — response
bodies, response headers, and the structured diagnostics the correlation
middleware emits — for the rejected operand values and for private-key
material shapes that never belong on any policy surface. A companion scan
pins the static contract artifacts: the committed OpenAPI snapshot and the
generated TypeScript client carry no operand or key material.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
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
from api_runtime.authentication_dependencies import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from api_runtime.exclusion_policy_composition import (
    OfflineExclusionPolicyState,
    compose_offline_exclusion_policy,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_os.diagnostics.events import EventName
from personal_os.runtime_configuration.models import RuntimeEnvironment

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
SNAPSHOT_PATH: Final[Path] = REPO_ROOT / "packages" / "api-client" / "openapi.json"
GENERATED_CLIENT_PATH: Final[Path] = (
    REPO_ROOT / "packages" / "api-client" / "src" / "generated" / "schema.ts"
)

ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN
_RULE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-0000000000a1")

#: Operand values a rejected draft rule carried; they must never render
#: again — not in the error envelope, the diagnostics or any later response.
REJECTED_OPERAND_SENTINELS: Final[tuple[str, ...]] = (
    "/absolute/escape/path",
    "Notes/*/Secret*Vault",
)

#: Material shapes that never belong on any policy surface.
PRIVATE_KEY_SENTINELS: Final[tuple[str, ...]] = (
    "BEGIN PRIVATE KEY",
    "BEGIN OPENSSH PRIVATE KEY",
    bytes(range(32)).hex(),
)


@dataclass
class _RecordingSink:
    """Diagnostic sink collecting every emitted event for the scan."""

    events: list[tuple[str, str]] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: dict[str, object]) -> None:
        self.events.append((event_name.value, json.dumps(fields, default=str)))

    def rendered(self) -> str:
        return "".join(f"{name}{payload}" for name, payload in self.events)


@dataclass(frozen=True, slots=True)
class _Exchange:
    label: str
    body: str
    headers: str


class _LeakJourney:
    """The full policy journey over the real composed application."""

    def __init__(self) -> None:
        self.sink = _RecordingSink()
        auth_state = OfflineAuthenticationState(totp_active=False)
        self.policy_state = OfflineExclusionPolicyState()
        self.application: FastAPI = create_api_application(
            environment=RuntimeEnvironment.TEST,
            readiness_probe=_ReadyProbe(),
            web_authentication=compose_offline_web_authentication(
                clock=OfflineAuthenticationClock(), state=auth_state
            ),
            exclusion_policy=compose_offline_exclusion_policy(state=self.policy_state),
            event_sink=self.sink,
        )
        self.exchanges: list[_Exchange] = []

    def run(self) -> None:
        with TestClient(self.application, base_url=ORIGIN) as client:
            self.client = client
            self._record("login", self._login())
            cookies = self._login_cookies
            rejected = client.put(
                "/api/admin/exclusion-policy/draft",
                headers=self._headers(cookies),
                json={
                    "expected_draft_version": 1,
                    "rules": [
                        {
                            "rule_id": str(_RULE_ID),
                            "rule_kind": "folder_prefix",
                            "folder_prefix": REJECTED_OPERAND_SENTINELS[0],
                        },
                        {
                            "rule_id": str(uuid4()),
                            "rule_kind": "path_glob",
                            "path_glob": REJECTED_OPERAND_SENTINELS[1],
                        },
                    ],
                },
            )
            self._record("draft-rejected", rejected)
            accepted = client.put(
                "/api/admin/exclusion-policy/draft",
                headers=self._headers(cookies),
                json={
                    "expected_draft_version": 1,
                    "rules": [
                        {
                            "rule_id": str(_RULE_ID),
                            "rule_kind": "folder_prefix",
                            "folder_prefix": "notes/private",
                        }
                    ],
                },
            )
            self._record("draft-accepted", accepted)
            preview = client.post(
                "/api/admin/exclusion-policy/previews", headers=self._headers(cookies)
            )
            self._record("preview-created", preview)
            ready_preview = self._force_ready(preview)
            published = client.post(
                "/api/admin/exclusion-policy/publications",
                headers={
                    **self._headers(cookies),
                    "X-Idempotency-Key": "leak-journey-001",
                },
                json={
                    "policy_preview_id": str(ready_preview.policy_preview_id),
                    "policy_draft_id": str(ready_preview.policy_draft_id),
                    "expected_draft_version": ready_preview.draft_version,
                    "expected_draft_sha256": ready_preview.draft_sha256,
                    "preview_impact_digest": str(ready_preview.impact_digest),
                    "expected_active_policy_revision_id": None,
                    "expected_active_revision_number": 0,
                    "confirmation": "PUBLISH EXCLUSION POLICY",
                },
            )
            self._record("published", published)
            credential = self._exchange_device()
            keysets = client.get(
                "/api/sync/exclusion-policy/keysets",
                headers={"Authorization": f"Bearer {credential}"},
            )
            self._record("keysets", keysets)
            snapshot = client.get(
                "/api/sync/exclusion-policy/snapshot",
                headers={"Authorization": f"Bearer {credential}"},
            )
            self._record("snapshot", snapshot)

    def _force_ready(self, response: object) -> object:
        from personal_os.exclusion_policy.previews import (
            PREVIEW_READY_EXPIRY_SECONDS,
            PreviewStatus,
            compute_impact_digest,
        )

        data = response.json()["data"]
        preview_id = UUID(str(data["policy_preview_id"]))
        record = self.policy_state.preview_rows[preview_id]
        ready = replace(
            record,
            status=PreviewStatus.READY,
            impact_digest=compute_impact_digest(()),
            ready_at=record.created_at,
            expires_at=record.created_at + timedelta(seconds=PREVIEW_READY_EXPIRY_SECONDS),
        )
        self.policy_state.preview_rows[preview_id] = ready
        return ready

    def _exchange_device(self) -> str:
        created = self.client.post(
            "/api/auth/device-authorizations",
            headers={"Origin": ORIGIN},
            json={
                "client_instance_id": str(uuid4()),
                "device_name": "Leak scanner",
                "platform_class": "obsidian_desktop",
                "platform_name": "linux",
                "plugin_version": "1.4.0",
                "requested_scope": "obsidian_sync",
            },
        )
        grant = dict(created.json()["data"])
        approved = self.client.post(
            f"/api/auth/device-authorizations/{grant['grant_id']}/approve",
            headers=self._headers(self._login_cookies),
        )
        assert approved.status_code == 200
        exchanged = self.client.post(
            f"/api/auth/device-authorizations/{grant['grant_id']}/poll",
            headers={"Authorization": f"Bearer {grant['polling_secret']}"},
        )
        return str(exchanged.json()["data"]["access_credential"])

    def _login(self) -> object:
        response = self.client.post(
            "/api/auth/login",
            headers={"Origin": ORIGIN},
            json={"username": "admin", "password": "correct-horse-battery-staple"},
        )
        assert response.status_code == 200, response.text
        self._login_cookies = {
            "session": response.cookies[SESSION_COOKIE_NAME],
            "csrf": response.cookies[CSRF_COOKIE_NAME],
        }
        return response

    def _headers(self, cookies: dict[str, str]) -> dict[str, str]:
        return {
            "Origin": ORIGIN,
            "Cookie": (
                f"{SESSION_COOKIE_NAME}={cookies['session']}; {CSRF_COOKIE_NAME}={cookies['csrf']}"
            ),
            "X-CSRF-Token": cookies["csrf"],
        }

    def _record(self, label: str, response: object) -> None:
        self.exchanges.append(
            _Exchange(
                label=label,
                body=str(response.text),
                headers=json.dumps(dict(response.headers), default=str),
            )
        )


class _ReadyProbe:
    """Readiness probe stub: the leak journey never consults it."""

    async def check(self) -> None: ...


@pytest.fixture
def journey() -> Iterator[_LeakJourney]:
    leak_journey = _LeakJourney()
    leak_journey.run()
    yield leak_journey


def test_rejected_operand_values_never_render_again(journey: _LeakJourney) -> None:
    for sentinel in REJECTED_OPERAND_SENTINELS:
        assert sentinel not in journey.sink.rendered()
        for exchange in journey.exchanges:
            if exchange.label == "draft-rejected":
                continue
            assert sentinel not in exchange.body, (sentinel, exchange.label)
            assert sentinel not in exchange.headers, (sentinel, exchange.label)
    rejected = next(e for e in journey.exchanges if e.label == "draft-rejected")
    for sentinel in REJECTED_OPERAND_SENTINELS:
        assert sentinel not in rejected.body


def test_no_private_key_material_renders_on_any_surface(journey: _LeakJourney) -> None:
    for sentinel in PRIVATE_KEY_SENTINELS:
        assert sentinel not in journey.sink.rendered()
        for exchange in journey.exchanges:
            assert sentinel not in exchange.body, (sentinel, exchange.label)
            assert sentinel not in exchange.headers, (sentinel, exchange.label)


def test_accepted_values_render_on_their_intended_surfaces_only(
    journey: _LeakJourney,
) -> None:
    # The scan is not vacuous: the accepted operand and the public keyset/
    # snapshot material render exactly on the draft, keyset and snapshot
    # responses that exist to carry them.
    accepted = next(e for e in journey.exchanges if e.label == "draft-accepted")
    assert "notes/private" in accepted.body
    keysets = next(e for e in journey.exchanges if e.label == "keysets")
    assert "exclusion_policy_keyset/v1" in keysets.body
    snapshot = next(e for e in journey.exchanges if e.label == "snapshot")
    assert "exclusion_policy_snapshot/v1" in snapshot.body
    assert "ed25519-sha256-" in snapshot.body
    published = next(e for e in journey.exchanges if e.label == "published")
    assert published.body  # 201 with the closed result


def test_contract_artifacts_carry_no_operand_or_key_material() -> None:
    for path in (SNAPSHOT_PATH, GENERATED_CLIENT_PATH):
        text = path.read_text(encoding="utf-8")
        for sentinel in (*REJECTED_OPERAND_SENTINELS, *PRIVATE_KEY_SENTINELS):
            assert sentinel not in text, (sentinel, path)
        assert "notes/private" not in text, path
