"""Authentication sentinel leak scan across every emitted surface (spec 24.7).

One journey drives the real composed application — factory, envelope handlers
and request correlation middleware — over the offline deterministic
authentication composition through every credential-bearing interaction:
rejected and accepted password login, TOTP enrollment start and verify,
rejected recovery, the full device grant create/lookup/approve/poll-exchange
chain, refresh rotation, the Admin device surface, self-revoke and password
change. Every exchanged byte is captured and scanned: response bodies,
response headers (including every ``Set-Cookie``), the structured diagnostics
the middleware emits, and the offline in-memory state — the one sanctioned
secret-bearing test double — where only hashes and ciphertext may exist.

Each sentinel names the exact response bodies (and, for cookie secrets, the
``Set-Cookie`` renderings) where the contract intends the value to appear;
every other occurrence is a leak. A companion scan pins the static surfaces:
the OpenAPI document, the generated TypeScript client, the committed blocklist
artifact, and the production Web and plugin bundles built by this gate.
"""

from __future__ import annotations

import base64
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import (
    OFFLINE_PASSWORD,
    OFFLINE_USERNAME,
    OFFLINE_WEB_ALLOWED_ORIGIN,
    OfflineAuthenticationClock,
    OfflineAuthenticationState,
    compose_offline_web_authentication,
)
from api_runtime.authentication_dependencies import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from fastapi.testclient import TestClient

from personal_os.authentication.totp import totp_code
from personal_os.diagnostics.events import EventName
from personal_os.runtime_configuration.models import RuntimeEnvironment

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

#: The versioned credential shape (spec 12.1): one of the three prefixes
#: followed by its UUID lookup id. Bare prefixes collide with minified vendor
#: text (``port1.onmessage``), so artifact scans pin the full shape.
_CREDENTIAL_SHAPE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:pg1|at1|rt1)\.[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
)

#: The one origin and base URL the offline composition accepts.
_ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN

# --- the sentinel vocabulary -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sentinel:
    """One secret value with the exact surfaces it may intentionally reach."""

    label: str
    value: str
    allowed_bodies: frozenset[str] = frozenset()
    allowed_set_cookie: bool = False


def _sentinel(
    label: str,
    value: str,
    *,
    allowed_bodies: frozenset[str] = (),
    allowed_set_cookie: bool = False,
) -> Sentinel:
    return Sentinel(
        label=label,
        value=value,
        allowed_bodies=frozenset(allowed_bodies),
        allowed_set_cookie=allowed_set_cookie,
    )


#: Client-presented sentinels: rejected credentials never echo anywhere.
_PASSWORD_SENTINEL: Final[Sentinel] = _sentinel(
    "rejected-password", "sentinel-rejected-password-4f7a-do-not-emit"
)
_NEW_PASSWORD_SENTINEL: Final[Sentinel] = _sentinel(
    "new-password", "sentinel-new-password-8c2d-do-not-emit"
)
_TOTP_CODE_SENTINEL: Final[Sentinel] = _sentinel("totp-code", "371949")
_RECOVERY_CODE_SENTINEL: Final[Sentinel] = _sentinel("recovery-code", "7EAK-5ENT-7INE")
_USER_CODE_SENTINEL: Final[Sentinel] = _sentinel("unknown-user-code", "7EAK-5ENT")
#: The device name renders only where the contract displays it to the operator.
_DEVICE_NAME_SENTINEL: Final[Sentinel] = _sentinel(
    "device-name",
    "Sentinel Leak Radar 4f7a",
    allowed_bodies=frozenset({"lookup", "admin-list"}),
)
_NAME_CONFIRMATION_SENTINEL: Final[Sentinel] = _sentinel(
    "revoke-confirmation", "Totally Wrong Device Name 4f7a"
)

#: Every client-presented sentinel must appear nowhere at all.
_CLIENT_SENTINELS: Final[tuple[Sentinel, ...]] = (
    _PASSWORD_SENTINEL,
    _NEW_PASSWORD_SENTINEL,
    _TOTP_CODE_SENTINEL,
    _RECOVERY_CODE_SENTINEL,
    _USER_CODE_SENTINEL,
    _NAME_CONFIRMATION_SENTINEL,
)

# The server-origin sentinels are registered by the journey itself: the
# one-time provisioning values the contract renders exactly once (user code,
# polling secret, TOTP secret and provisioning URI, recovery codes, the
# exchanged and refreshed credentials) and the two cookie secrets, which may
# only ever travel inside ``Set-Cookie``.


# --- exchange capture and diagnostics capture -------------------------------------------


@dataclass(slots=True)
class CapturedExchange:
    """One recorded HTTP response: its label, rendered headers and body."""

    label: str
    headers: str
    body: str

    @property
    def set_cookie_lines(self) -> str:
        return "\n".join(
            line for line in self.headers.splitlines() if line.lower().startswith("set-cookie:")
        )

    @property
    def headers_without_set_cookie(self) -> str:
        return "\n".join(
            line for line in self.headers.splitlines() if not line.lower().startswith("set-cookie:")
        )


@dataclass
class RecordingEventSink:
    """Structured-diagnostics capture retaining every emitted event verbatim."""

    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None:
        self.events.append((event_name.value, dict(fields or {})))

    def rendered(self) -> str:
        import json

        return json.dumps(self.events, default=str)


class LeakJourney:
    """The full credential journey against one offline composed application."""

    def __init__(self) -> None:
        self.clock = OfflineAuthenticationClock()
        self.state = OfflineAuthenticationState(totp_active=False)
        self.sink = RecordingEventSink()
        runtime = compose_offline_web_authentication(clock=self.clock, state=self.state)
        application = create_api_application(
            environment=RuntimeEnvironment.TEST,
            readiness_probe=_ReadyProbe(),
            web_authentication=runtime,
            event_sink=self.sink,
        )
        self.client = TestClient(application, base_url=_ORIGIN)
        self.exchanges: list[CapturedExchange] = []
        self.sentinels: dict[str, Sentinel] = {
            each.label: each for each in (*_CLIENT_SENTINELS, _DEVICE_NAME_SENTINEL)
        }
        self.cookies: dict[str, str] = {}

    # -- capture -------------------------------------------------------------------

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
        self,
        label: str,
        value: str,
        *,
        allowed_bodies: frozenset[str] = frozenset(),
        allowed_set_cookie: bool = False,
    ) -> None:
        assert value, f"sentinel '{label}' must carry a real captured value"
        self.sentinels[label] = _sentinel(
            label,
            value,
            allowed_bodies=allowed_bodies,
            allowed_set_cookie=allowed_set_cookie,
        )

    def capture_cookie_sentinels(self, response: Any, label: str) -> None:
        """Register the rotated cookie secrets as Set-Cookie-only sentinels."""
        for cookie_name, sentinel_label in (
            (SESSION_COOKIE_NAME, f"session-secret[{label}]"),
            (CSRF_COOKIE_NAME, f"csrf-secret[{label}]"),
        ):
            value = response.cookies.get(cookie_name)
            if value:
                self.register_sentinel(sentinel_label, value, allowed_set_cookie=True)

    def remember_cookies(self, response: Any) -> None:
        self.cookies = {
            "session": response.cookies[SESSION_COOKIE_NAME],
            "csrf": response.cookies[CSRF_COOKIE_NAME],
        }

    # -- request helpers -----------------------------------------------------------

    def authenticated_headers(self, *, origin: str = _ORIGIN) -> dict[str, str]:
        return {
            "Origin": origin,
            "Cookie": (
                f"{SESSION_COOKIE_NAME}={self.cookies['session']}; "
                f"{CSRF_COOKIE_NAME}={self.cookies['csrf']}"
            ),
            "X-CSRF-Token": self.cookies["csrf"],
        }

    # -- the journey ---------------------------------------------------------------

    def run(self) -> None:
        self._login_and_session_surfaces()
        self._totp_surfaces()
        self._device_surfaces()
        self._admin_and_terminal_surfaces()

    def _login_and_session_surfaces(self) -> None:
        rejected = self.request(
            "login-rejected",
            "POST",
            "/api/auth/login",
            headers={"Origin": _ORIGIN},
            json_body={
                "username": OFFLINE_USERNAME,
                "password": _PASSWORD_SENTINEL.value,
            },
        )
        assert rejected.status_code == 401, rejected.text

        login = self.request(
            "login",
            "POST",
            "/api/auth/login",
            headers={"Origin": _ORIGIN},
            json_body={"username": OFFLINE_USERNAME, "password": OFFLINE_PASSWORD},
        )
        assert login.status_code == 200, login.text
        self.capture_cookie_sentinels(login, "login")
        self.remember_cookies(login)

        reauth_rejected = self.request(
            "reauthenticate-rejected",
            "POST",
            "/api/auth/reauthenticate",
            headers=self.authenticated_headers(),
            json_body={"password": _PASSWORD_SENTINEL.value},
        )
        assert reauth_rejected.status_code == 401, reauth_rejected.text

    def _totp_surfaces(self) -> None:
        enroll = self.request(
            "enroll-start",
            "POST",
            "/api/auth/totp/enrollments",
            headers=self.authenticated_headers(),
            json_body={"action": "start"},
        )
        assert enroll.status_code == 200, enroll.text
        enrollment = enroll.json()["data"]["enrollment"]
        enrollment_id = str(enrollment["enrollment_id"])
        secret_base32 = str(enrollment["secret"])
        provisioning_uri = str(enrollment["provisioning_uri"])
        self.register_sentinel("totp-secret", secret_base32, allowed_bodies={"enroll-start"})
        self.register_sentinel(
            "provisioning-uri", provisioning_uri, allowed_bodies={"enroll-start"}
        )
        secret = base64.b32decode(secret_base32)
        correct_code = totp_code(
            secret=secret, unix_time_seconds=int(self.clock.database_now_value.timestamp())
        )
        wrong_code = _TOTP_CODE_SENTINEL.value
        if wrong_code == correct_code:
            # The offline composition is deterministic but the enrollment
            # secret is fresh each run: keep the sentinel distinct from the
            # one accepted code so it exercises the rejection, not the pass.
            wrong_code = f"{(int(correct_code) + 1) % 1_000_000:06d}"
            self.sentinels["totp-code"] = _sentinel("totp-code", wrong_code)

        verify_rejected = self.request(
            "enroll-verify-rejected",
            "POST",
            f"/api/auth/totp/enrollments/{enrollment_id}/verify",
            headers=self.authenticated_headers(),
            json_body={"code": self.sentinels["totp-code"].value},
        )
        assert verify_rejected.status_code == 401, verify_rejected.text

        verified = self.request(
            "enroll-verify",
            "POST",
            f"/api/auth/totp/enrollments/{enrollment_id}/verify",
            headers=self.authenticated_headers(),
            json_body={"code": correct_code},
        )
        assert verified.status_code == 200, verified.text
        issued_codes = [str(code) for code in verified.json()["data"]["codes"]]
        assert issued_codes
        for index, code in enumerate(issued_codes):
            self.register_sentinel(
                f"recovery-code[{index}]", code, allowed_bodies={"enroll-verify"}
            )

        recovery_rejected = self.request(
            "recovery-rejected",
            "POST",
            "/api/auth/totp/recovery",
            headers=self.authenticated_headers(),
            json_body={
                "password": _PASSWORD_SENTINEL.value,
                "recovery_code": _RECOVERY_CODE_SENTINEL.value,
            },
        )
        assert recovery_rejected.status_code == 401, recovery_rejected.text

    def _device_surfaces(self) -> None:
        created = self.request(
            "grant-create",
            "POST",
            "/api/auth/device-authorizations",
            headers={"Origin": _ORIGIN},
            json_body={
                "client_instance_id": str(uuid4()),
                "device_name": _DEVICE_NAME_SENTINEL.value,
                "platform_class": "obsidian_desktop",
                "platform_name": "windows",
                "plugin_version": "1.4.0",
                "requested_scope": "obsidian_sync",
            },
        )
        assert created.status_code == 200, created.text
        grant = created.json()["data"]
        grant_id = str(grant["grant_id"])
        self.register_sentinel(
            "user-code", str(grant["user_code"]), allowed_bodies={"grant-create", "lookup"}
        )
        self.register_sentinel(
            "polling-secret", str(grant["polling_secret"]), allowed_bodies={"grant-create"}
        )

        lookup_rejected = self.request(
            "lookup-rejected",
            "POST",
            "/api/auth/device-authorizations/lookup",
            headers=self.authenticated_headers(),
            json_body={"user_code": _USER_CODE_SENTINEL.value},
        )
        assert lookup_rejected.status_code in (401, 404), lookup_rejected.text

        lookup = self.request(
            "lookup",
            "POST",
            "/api/auth/device-authorizations/lookup",
            headers=self.authenticated_headers(),
            json_body={"user_code": str(grant["user_code"])},
        )
        assert lookup.status_code == 200, lookup.text

        approved = self.request(
            "approve",
            "POST",
            f"/api/auth/device-authorizations/{grant_id}/approve",
            headers=self.authenticated_headers(),
        )
        assert approved.status_code == 200, approved.text

        exchanged = self.request(
            "poll-exchange",
            "POST",
            f"/api/auth/device-authorizations/{grant_id}/poll",
            headers={"Authorization": f"Bearer {grant['polling_secret']}"},
        )
        assert exchanged.status_code == 200, exchanged.text
        credentials = exchanged.json()["data"]
        self.register_sentinel(
            "access-credential",
            str(credentials["access_credential"]),
            allowed_bodies={"poll-exchange"},
        )
        self.register_sentinel(
            "refresh-credential",
            str(credentials["refresh_credential"]),
            allowed_bodies={"poll-exchange"},
        )

        refreshed = self.request(
            "refresh",
            "POST",
            "/api/auth/device-tokens/refresh",
            headers={"Authorization": f"Bearer {credentials['refresh_credential']}"},
            json_body={"rotation_id": str(uuid4())},
        )
        assert refreshed.status_code == 200, refreshed.text
        successor = refreshed.json()["data"]
        self.register_sentinel(
            "refreshed-access-credential",
            str(successor["access_credential"]),
            allowed_bodies={"refresh"},
        )
        self.register_sentinel(
            "refreshed-refresh-credential",
            str(successor["refresh_credential"]),
            allowed_bodies={"refresh"},
        )

    def _admin_and_terminal_surfaces(self) -> None:
        listed = self.request(
            "admin-list",
            "GET",
            "/api/admin/devices",
            headers=self.authenticated_headers(),
        )
        assert listed.status_code == 200, listed.text
        devices = listed.json()["data"]["devices"]
        registered = [device for device in devices if device["status"] == "active"]
        assert registered, "the exchanged device must appear in the Admin list"
        device_id = str(registered[0]["device_id"])

        revoke_rejected = self.request(
            "admin-revoke-rejected",
            "POST",
            f"/api/admin/devices/{device_id}/revoke",
            headers=self.authenticated_headers(),
            json_body={"device_name_confirmation": _NAME_CONFIRMATION_SENTINEL.value},
        )
        assert revoke_rejected.status_code == 409, revoke_rejected.text

        revoked = self.request(
            "revoke-current",
            "POST",
            "/api/auth/device-tokens/revoke-current",
            headers={
                "Authorization": f"Bearer {self.sentinels['refreshed-refresh-credential'].value}"
            },
        )
        assert revoked.status_code == 200, revoked.text

        changed = self.request(
            "password-change",
            "PUT",
            "/api/auth/password",
            headers=self.authenticated_headers(),
            json_body={"new_password": _NEW_PASSWORD_SENTINEL.value},
        )
        assert changed.status_code == 200, changed.text

    # -- offline state render --------------------------------------------------------

    def rendered_offline_state(self) -> str:
        """Render every offline row, skipping the sanctioned name columns."""

        def render(value: Any) -> str:
            import json

            return json.dumps(value, default=str)

        grant_rows = []
        for row in self.state.device_grant_rows:
            grant_rows.append(
                {
                    "user_code_hash": row.user_code_hash,
                    "polling_secret_hash": row.polling_secret_hash,
                    "platform_name": row.platform_name,
                    "plugin_version": row.plugin_version,
                    "state": str(row.state),
                }
            )
        token_rows = [
            {"secret_hash": row.secret_hash, "token_kind": row.token_kind}
            for row in self.state.device_token_rows
        ]
        session_rows = [
            {
                "session_secret_hash": row.session_secret_hash,
                "csrf_secret_hash": row.csrf_secret_hash,
            }
            for row in self.state.sessions_by_secret_hash.values()
        ]
        totp_rows = [
            {
                "sealed": {
                    "key_id": row.sealed.key_id,
                    "nonce": row.sealed.nonce,
                    "ciphertext": row.sealed.ciphertext,
                }
            }
            for row in self.state.totp_credential_rows
        ]
        recovery_rows = [{"code_hash": row.code_hash} for row in self.state.recovery_code_rows]
        return render(
            {
                "grants": grant_rows,
                "tokens": token_rows,
                "sessions": session_rows,
                "totp": totp_rows,
                "recovery": recovery_rows,
                "throttle_keys": sorted(self.state.buckets),
                "audit_actions": self.state.device_grant_audit_actions
                + self.state.device_exchange_audit_actions
                + self.state.device_revoke_audit_actions,
            }
        )


class _ReadyProbe:
    """Readiness probe stub: the authentication routes never consult it."""

    async def check(self) -> None: ...


# --- the scan -----------------------------------------------------------------------------


def assert_no_sentinel_leak(journey: LeakJourney) -> None:
    """Scan every captured surface for every sentinel outside its allowance."""
    for sentinel in journey.sentinels.values():
        for exchange in journey.exchanges:
            if exchange.label not in sentinel.allowed_bodies:
                assert sentinel.value not in exchange.body, (
                    f"sentinel '{sentinel.label}' leaked into the body of "
                    f"'{exchange.label}': {sentinel.value!r}"
                )
            assert sentinel.value not in exchange.headers_without_set_cookie, (
                f"sentinel '{sentinel.label}' leaked into the headers of "
                f"'{exchange.label}': {sentinel.value!r}"
            )
            if not sentinel.allowed_set_cookie:
                assert sentinel.value not in exchange.set_cookie_lines, (
                    f"sentinel '{sentinel.label}' leaked into a Set-Cookie of '{exchange.label}'"
                )
        assert sentinel.value not in journey.sink.rendered(), (
            f"sentinel '{sentinel.label}' leaked into the structured diagnostics"
        )
        assert sentinel.value not in journey.rendered_offline_state(), (
            f"sentinel '{sentinel.label}' exists in plaintext inside the offline state"
        )


def test_full_credential_journey_leaks_no_sentinel_on_any_surface() -> None:
    journey = LeakJourney()
    journey.run()
    assert_no_sentinel_leak(journey)

    # The capture is real: the journey crossed every intended surface and the
    # middleware observed the exchanges, so an empty scan cannot pass silently.
    labels = {exchange.label for exchange in journey.exchanges}
    expected_labels = {
        "login-rejected",
        "login",
        "reauthenticate-rejected",
        "enroll-start",
        "enroll-verify-rejected",
        "enroll-verify",
        "recovery-rejected",
        "grant-create",
        "lookup-rejected",
        "lookup",
        "approve",
        "poll-exchange",
        "refresh",
        "admin-list",
        "admin-revoke-rejected",
        "revoke-current",
        "password-change",
    }
    assert expected_labels <= labels
    observed_events = {name for name, _ in journey.sink.events}
    assert "api_request_completed" in observed_events


def test_intended_rendering_surfaces_still_render_their_values() -> None:
    """The exempted surfaces really carry their values (the scan is not vacuous)."""
    journey = LeakJourney()
    journey.run()
    bodies = {exchange.label: exchange.body for exchange in journey.exchanges}
    assert journey.sentinels["totp-secret"].value in bodies["enroll-start"]
    assert journey.sentinels["user-code"].value in bodies["lookup"]
    assert journey.sentinels["device-name"].value in bodies["lookup"]
    assert journey.sentinels["device-name"].value in bodies["admin-list"]
    assert journey.sentinels["access-credential"].value in bodies["poll-exchange"]
    assert journey.sentinels["refresh-credential"].value in bodies["poll-exchange"]
    assert journey.sentinels["refreshed-refresh-credential"].value in bodies["refresh"]
    assert journey.sentinels["recovery-code[0]"].value in bodies["enroll-verify"]
    login = next(exchange for exchange in journey.exchanges if exchange.label == "login")
    assert journey.sentinels["session-secret[login]"].value in login.set_cookie_lines
    # The polling secret renders exactly once at creation and never again.
    non_provisioning_bodies = "".join(
        exchange.body for exchange in journey.exchanges if exchange.label != "grant-create"
    )
    assert journey.sentinels["polling-secret"].value not in non_provisioning_bodies
    assert journey.sentinels["polling-secret"].value not in journey.sink.rendered()
    # No client-presented sentinel ever echoes anywhere at all.
    for sentinel in _CLIENT_SENTINELS:
        assert sentinel.value not in journey.sink.rendered()
        for exchange in journey.exchanges:
            assert sentinel.value not in exchange.body
            assert sentinel.value not in exchange.headers


# --- static artifact scan ------------------------------------------------------------------


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
        except UnicodeDecodeError, OSError:
            continue
    return scanned


def test_generated_and_committed_artifacts_carry_no_credential_material() -> None:
    scanned: list[tuple[Path, str]] = []
    scanned += _scan_files(
        REPO_ROOT / "packages" / "api-client",
        frozenset({"node_modules", "coverage"}),
    )
    scanned += _scan_files(
        REPO_ROOT / "src" / "personal_os" / "authentication" / "data", frozenset()
    )
    # The versioned credential prefixes (in their real credential shape: the
    # prefix followed by a UUID lookup id) and every client sentinel must stay
    # absent from the contract document, the generated client and the
    # committed blocklist artifact (digests only).
    forbidden_literals = [each.value for each in _CLIENT_SENTINELS]
    for path, text in scanned:
        assert _CREDENTIAL_SHAPE_PATTERN.search(text) is None, (
            f"a versioned credential form leaked into {path}"
        )
        for needle in forbidden_literals:
            assert needle not in text, f"{needle!r} leaked into {path}"


def _run_pnpm_filter(package_name: str, script_name: str) -> None:
    import shutil
    import sys

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


def test_production_web_and_plugin_bundles_carry_no_credential_material() -> None:
    _run_pnpm_filter("@workspace/web-runtime", "build")
    _run_pnpm_filter("@workspace/obsidian-plugin", "build")

    forbidden_literals = [each.value for each in _CLIENT_SENTINELS]
    web_bundles = _scan_files(REPO_ROOT / "apps" / "web" / ".next", frozenset({"cache", "dev"}))
    plugin_bundles = _scan_files(REPO_ROOT / "apps" / "obsidian-plugin" / "dist", frozenset())
    assert web_bundles and plugin_bundles, "both production bundles must exist for the scan"
    for path, text in web_bundles + plugin_bundles:
        # The credential prefixes alone collide with minified vendor text
        # (``port1.onmessage``), so bundles are pinned on the full credential
        # shape: the versioned prefix followed by its UUID lookup id.
        assert _CREDENTIAL_SHAPE_PATTERN.search(text) is None, (
            f"a versioned credential form leaked into the production bundle {path}"
        )
        for needle in forbidden_literals:
            assert needle not in text, f"{needle!r} leaked into the production bundle {path}"
