"""Sensitive policy-material leak scan across every emitted surface (spec 23.5).

One journey drives the real composed application — factory, envelope handlers
and correlation middleware — over the offline deterministic policy
composition through the error-bearing exchanges where a leak would escape:
a rejected draft whose operand must never echo, a publication attempt with a
mistyped confirmation, a preview read of an unknown preview, a successful
publication, and the plugin keyset and snapshot reads. Every exchanged byte
is captured — response bodies, response headers, the structured diagnostics
the middleware emits and the Python stdlib log records emitted during the
journey — and scanned for the rejected locator/operand sentinels, for the
signature value (which may exist only inside the plugin snapshot body that
exists to carry it) and for private-key material shapes.

A companion static scan pins the generated artifacts (the committed OpenAPI
snapshot and the generated TypeScript client) and the production Web and
plugin bundles built by this gate: no operand sentinel, no signature value
and no PEM private-key header may exist in any of them.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
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
from api_runtime.exclusion_policy_composition import (
    OfflineExclusionPolicyState,
    compose_offline_exclusion_policy,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_os.diagnostics.events import EventName
from personal_os.runtime_configuration.models import RuntimeEnvironment

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
OPENAPI_SNAPSHOT_PATH: Final[Path] = REPO_ROOT / "packages" / "api-client" / "openapi.json"
GENERATED_CLIENT_PATH: Final[Path] = (
    REPO_ROOT / "packages" / "api-client" / "src" / "generated" / "schema.ts"
)

ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN
_RULE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-0000000000b1")

#: Rejected locator/operand values the failed draft carried; they must never
#: render again — not in the error envelope, the diagnostics, a log record
#: or any later response.
REJECTED_OPERAND_SENTINELS: Final[tuple[str, ...]] = (
    "/absolute/sentinel/escape/path",
    "Sentinel/*/?Forbidden[Glob]",
)

#: Material shapes that never belong on any policy surface.
PRIVATE_KEY_SENTINELS: Final[tuple[str, ...]] = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "BEGIN EC PRIVATE KEY",
)


@dataclass(frozen=True, slots=True)
class Sentinel:
    """One sensitive value with the exact bodies it may intentionally reach."""

    label: str
    value: str
    allowed_bodies: frozenset[str] = frozenset()


@dataclass
class CapturedLogRecord:
    """One stdlib log record rendered verbatim for the scan."""

    level: str
    name: str
    message: str

    def rendered(self) -> str:
        return f"{self.level}{self.name}{self.message}"


@dataclass
class CapturedExchange:
    """One recorded HTTP response: label, rendered headers and body."""

    label: str
    headers: str
    body: str


@dataclass
class RecordingEventSink:
    """Structured-diagnostics capture retaining every emitted event verbatim."""

    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None:
        self.events.append((event_name.value, dict(fields or {})))

    def rendered(self) -> str:
        return json.dumps(self.events, default=str)


class RecordingLogHandler(logging.Handler):
    """Root-logger capture of every Python log record emitted mid-journey."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[CapturedLogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.exc_info:
            import traceback

            message += traceback.format_exception(*record.exc_info)[-1]
        self.records.append(CapturedLogRecord(record.levelname, record.name, message))

    def rendered(self) -> str:
        return "".join(record.rendered() for record in self.records)


class SensitivePolicyJourney:
    """The policy error-and-plugin journey against one offline composition."""

    def __init__(self) -> None:
        self.sink = RecordingEventSink()
        self.log_handler = RecordingLogHandler()
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
        self.client = TestClient(self.application, base_url=ORIGIN)
        self.exchanges: list[CapturedExchange] = []
        self.sentinels: dict[str, Sentinel] = {
            sentinel.label: sentinel
            for sentinel in (
                Sentinel(label=f"rejected-operand[{index}]", value=value)
                for index, value in enumerate(REJECTED_OPERAND_SENTINELS)
            )
        }
        self.cookies: dict[str, str] = {}

    # -- capture ------------------------------------------------------------------

    def request(
        self,
        label: str,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        response = self.client.request(method, path, headers=headers, json=json_body)
        rendered_headers = "\n".join(f"{name}: {value}" for name, value in response.headers.items())
        self.exchanges.append(
            CapturedExchange(label=label, headers=rendered_headers, body=response.text)
        )
        return response

    def register_sentinel(
        self, label: str, value: str, *, allowed_bodies: frozenset[str] = frozenset()
    ) -> None:
        assert value, f"sentinel '{label}' must carry a real captured value"
        self.sentinels[label] = Sentinel(label=label, value=value, allowed_bodies=allowed_bodies)

    # -- request helpers ------------------------------------------------------------

    def authenticated_headers(self) -> dict[str, str]:
        return {
            "Origin": ORIGIN,
            "Cookie": (
                f"{SESSION_COOKIE_NAME}={self.cookies['session']}; "
                f"{CSRF_COOKIE_NAME}={self.cookies['csrf']}"
            ),
            "X-CSRF-Token": self.cookies["csrf"],
        }

    # -- the journey ------------------------------------------------------------------

    def run(self) -> None:
        root_logger = logging.getLogger()
        previous_level = root_logger.level
        root_logger.addHandler(self.log_handler)
        root_logger.setLevel(logging.NOTSET)
        try:
            self._run_exchanges()
        finally:
            root_logger.removeHandler(self.log_handler)
            root_logger.setLevel(previous_level)

    def _run_exchanges(self) -> None:
        login = self.request(
            "login",
            "POST",
            "/api/auth/login",
            headers={"Origin": ORIGIN},
            json_body={"username": "admin", "password": "correct-horse-battery-staple"},
        )
        assert login.status_code == 200, login.text
        self.cookies = {
            "session": login.cookies[SESSION_COOKIE_NAME],
            "csrf": login.cookies[CSRF_COOKIE_NAME],
        }

        rejected = self.request(
            "draft-rejected",
            "PUT",
            "/api/admin/exclusion-policy/draft",
            headers=self.authenticated_headers(),
            json_body={
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
        assert rejected.status_code in (400, 422), rejected.text

        accepted = self.request(
            "draft-accepted",
            "PUT",
            "/api/admin/exclusion-policy/draft",
            headers=self.authenticated_headers(),
            json_body={
                "expected_draft_version": 1,
                "rules": [
                    {
                        "rule_id": str(_RULE_ID),
                        "rule_kind": "folder_prefix",
                        "folder_prefix": "notes/sentinel-allowed",
                    }
                ],
            },
        )
        assert accepted.status_code == 200, accepted.text

        preview = self.request(
            "preview-created",
            "POST",
            "/api/admin/exclusion-policy/previews",
            headers=self.authenticated_headers(),
        )
        assert preview.status_code == 202, preview.text

        unknown_preview = self.request(
            "preview-unknown",
            "GET",
            f"/api/admin/exclusion-policy/previews/{uuid4()}",
            headers=self.authenticated_headers(),
        )
        assert unknown_preview.status_code in (403, 404, 409), unknown_preview.text

        ready = self._force_ready(preview)
        misconfirmed = self.request(
            "publication-misconfirmed",
            "POST",
            "/api/admin/exclusion-policy/publications",
            headers={**self.authenticated_headers(), "X-Idempotency-Key": "sentinel-journey-001"},
            json_body={
                "policy_preview_id": str(ready.policy_preview_id),
                "policy_draft_id": str(ready.policy_draft_id),
                "expected_draft_version": ready.draft_version,
                "expected_draft_sha256": str(ready.draft_sha256),
                "preview_impact_digest": str(ready.impact_digest),
                "expected_active_policy_revision_id": None,
                "expected_active_revision_number": 0,
                "confirmation": "publish exclusion policy (wrong)",
            },
        )
        assert misconfirmed.status_code in (400, 403, 409, 422), misconfirmed.text

        published = self.request(
            "published",
            "POST",
            "/api/admin/exclusion-policy/publications",
            headers={**self.authenticated_headers(), "X-Idempotency-Key": "sentinel-journey-002"},
            json_body={
                "policy_preview_id": str(ready.policy_preview_id),
                "policy_draft_id": str(ready.policy_draft_id),
                "expected_draft_version": ready.draft_version,
                "expected_draft_sha256": str(ready.draft_sha256),
                "preview_impact_digest": str(ready.impact_digest),
                "expected_active_policy_revision_id": None,
                "expected_active_revision_number": 0,
                "confirmation": "PUBLISH EXCLUSION POLICY",
            },
        )
        assert published.status_code == 201, published.text

        credential = self._exchange_device()
        keysets = self.request(
            "keysets",
            "GET",
            "/api/sync/exclusion-policy/keysets",
            headers={"Authorization": f"Bearer {credential}"},
        )
        assert keysets.status_code == 200, keysets.text
        snapshot = self.request(
            "snapshot",
            "GET",
            "/api/sync/exclusion-policy/snapshot",
            headers={"Authorization": f"Bearer {credential}"},
        )
        assert snapshot.status_code == 200, snapshot.text

        # The snapshot's detached signature value exists only inside the
        # snapshot body that exists to carry it; everywhere else it is a leak.
        signature_value = str(snapshot.json()["data"]["signature"]["value"])
        assert signature_value
        self.register_sentinel(
            "published-signature", signature_value, allowed_bodies=frozenset({"snapshot"})
        )

    def _force_ready(self, response: Any) -> Any:
        from dataclasses import replace

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
                "device_name": "Policy leak scanner",
                "platform_class": "obsidian_desktop",
                "platform_name": "linux",
                "plugin_version": "1.4.0",
                "requested_scope": "obsidian_sync",
            },
        )
        grant = dict(created.json()["data"])
        approved = self.client.post(
            f"/api/auth/device-authorizations/{grant['grant_id']}/approve",
            headers=self.authenticated_headers(),
        )
        assert approved.status_code == 200
        exchanged = self.client.post(
            f"/api/auth/device-authorizations/{grant['grant_id']}/poll",
            headers={"Authorization": f"Bearer {grant['polling_secret']}"},
        )
        return str(exchanged.json()["data"]["access_credential"])


class _ReadyProbe:
    """Readiness probe stub: the leak journey never consults it."""

    async def check(self) -> None: ...


@pytest.fixture
def journey() -> Iterator[SensitivePolicyJourney]:
    leak_journey = SensitivePolicyJourney()
    leak_journey.run()
    yield leak_journey


def _assert_no_sentinel_leak(journey: SensitivePolicyJourney) -> None:
    for sentinel in journey.sentinels.values():
        for exchange in journey.exchanges:
            if exchange.label not in sentinel.allowed_bodies:
                assert sentinel.value not in exchange.body, (
                    f"sentinel '{sentinel.label}' leaked into the body of '{exchange.label}'"
                )
            assert sentinel.value not in exchange.headers, (
                f"sentinel '{sentinel.label}' leaked into the headers of '{exchange.label}'"
            )
        assert sentinel.value not in journey.sink.rendered(), (
            f"sentinel '{sentinel.label}' leaked into the structured diagnostics"
        )
        assert sentinel.value not in journey.log_handler.rendered(), (
            f"sentinel '{sentinel.label}' leaked into a Python log record"
        )


def test_policy_error_journey_leaks_no_operand_or_signature() -> None:
    leak_journey = SensitivePolicyJourney()
    leak_journey.run()
    _assert_no_sentinel_leak(leak_journey)

    # The scan is not vacuous: every intended exchange happened and the
    # middleware observed them.
    labels = {exchange.label for exchange in leak_journey.exchanges}
    expected_labels = {
        "login",
        "draft-rejected",
        "draft-accepted",
        "preview-created",
        "preview-unknown",
        "publication-misconfirmed",
        "published",
        "keysets",
        "snapshot",
    }
    assert labels == expected_labels
    observed_events = {name for name, _ in leak_journey.sink.events}
    assert "api_request_completed" in observed_events
    # The rejected draft really was rejected and the accepted operand really
    # renders where the contract displays it.
    rejected = next(e for e in leak_journey.exchanges if e.label == "draft-rejected")
    assert "exclusion_policy_input_invalid" in rejected.body
    accepted = next(e for e in leak_journey.exchanges if e.label == "draft-accepted")
    assert "notes/sentinel-allowed" in accepted.body


def test_private_key_material_never_reaches_any_surface(journey: SensitivePolicyJourney) -> None:
    for sentinel_value in PRIVATE_KEY_SENTINELS:
        assert sentinel_value not in journey.sink.rendered()
        assert sentinel_value not in journey.log_handler.rendered()
        for exchange in journey.exchanges:
            assert sentinel_value not in exchange.body, (sentinel_value, exchange.label)
            assert sentinel_value not in exchange.headers, (sentinel_value, exchange.label)


def test_signature_value_exists_only_in_the_snapshot_surface(
    journey: SensitivePolicyJourney,
) -> None:
    signature = journey.sentinels["published-signature"].value
    snapshot = next(e for e in journey.exchanges if e.label == "snapshot")
    assert signature in snapshot.body
    non_snapshot = "".join(
        exchange.body for exchange in journey.exchanges if exchange.label != "snapshot"
    )
    assert signature not in non_snapshot
    assert signature not in journey.sink.rendered()
    assert signature not in journey.log_handler.rendered()


# --- static artifact and production-bundle scans -----------------------------------------


def _scan_files(root: Path, excluded_directory_names: frozenset[str]) -> list[tuple[Path, str]]:
    """Collect the text of every bounded text file beneath one root."""
    scanned: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if excluded_directory_names & set(path.parts):
            continue
        try:
            scanned.append((path, path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return scanned


def test_generated_contract_artifacts_carry_no_policy_material() -> None:
    for path in (OPENAPI_SNAPSHOT_PATH, GENERATED_CLIENT_PATH):
        text = path.read_text(encoding="utf-8")
        for needle in (*REJECTED_OPERAND_SENTINELS, *PRIVATE_KEY_SENTINELS):
            assert needle not in text, (needle, path)
        assert "notes/sentinel-allowed" not in text, path


def _run_pnpm_filter(package_name: str, script_name: str) -> None:
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        pytest.fail("pnpm is required to build the production bundles for the leak gate")
    command = [pnpm, "--filter", package_name, "run", script_name]
    if sys.platform == "win32":
        command = ["cmd.exe", "/c", pnpm, *command[1:]]
    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"building {package_name} failed:\n{completed.stdout}\n{completed.stderr}"
    )


def test_production_web_and_plugin_bundles_carry_no_policy_material(
    journey: SensitivePolicyJourney,
) -> None:
    _run_pnpm_filter("@workspace/web-runtime", "build")
    _run_pnpm_filter("@workspace/obsidian-plugin", "build")

    forbidden_literals: list[str] = [*REJECTED_OPERAND_SENTINELS, *PRIVATE_KEY_SENTINELS]
    for sentinel in journey.sentinels.values():
        forbidden_literals.append(sentinel.value)
    web_bundles = _scan_files(REPO_ROOT / "apps" / "web" / ".next", frozenset({"cache", "dev"}))
    plugin_bundles = _scan_files(REPO_ROOT / "apps" / "obsidian-plugin" / "dist", frozenset())
    assert web_bundles and plugin_bundles, "both production bundles must exist for the scan"
    for path, text in web_bundles + plugin_bundles:
        for needle in forbidden_literals:
            assert needle not in text, f"{needle!r} leaked into the production bundle {path}"
