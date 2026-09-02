"""Privacy contract of the source-conflict surfaces (Child 8 spec 9 / spec 8.10).

Distinct sentinels ride every private value of the conflict flow — the raw
vault locator, merged-draft text, candidate bytes, bearer token, canonical
object key, presigned-URL shape, full content digest and idempotency key —
and none may ever surface in a wire response, a typed error rendering, a
diagnostic event or a response header. The journeys drive the REAL
application factory over the offline compositions: the journal preflight
and conflict-content capture lane with sentinel locator and sentinel
candidate bytes, the Conflict Inbox listing/detail/resolve lanes over
sentinel evidence bytes, and the verified evidence read — whose exact bytes
ARE the product, so only its headers are scanned. The one exception the
contract pins deliberately: no surface ever emits the digest, key, URL or
locator, because no member exists to carry one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from io import StringIO
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
from api_runtime.small_file_sync_composition import (
    OfflineSmallFileSyncState,
    compose_offline_small_file_sync,
)
from api_runtime.source_conflict_composition import (
    OfflineSourceConflictState,
    compose_offline_source_conflicts,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_os.diagnostics.events import EventName
from personal_os.diagnostics.logging import (
    DiagnosticLogger,
    configure_diagnostics,
    reset_diagnostics_for_testing,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.source_conflicts.commands import ConflictResolutionResult
from personal_os.source_conflicts.contracts import (
    ConflictCandidate,
    ConflictKind,
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
_WORKSPACE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000002")
_CONFLICT_ID: Final[UUID] = uuid4()
_CAPTURED_AT: Final[datetime] = datetime(2026, 9, 2, 9, 15, 0, tzinfo=UTC)

#: The distinct sentinel family: raw locator path, merged-draft diff text,
#: candidate bytes, a bearer token, a canonical object key, a presigned-URL
#: shape, one full 64-hex content digest and one idempotency key.
SENTINEL_LOCATOR: Final[str] = "notes/do-not-emit-conflict-locator.md"
SENTINEL_DRAFT: Final[str] = "<<<<<<< HEAD do-not-emit-merged-diff-text ======="
SENTINEL_CANDIDATE_BYTES: Final[bytes] = b"do-not-emit-conflict-candidate-bytes"
SENTINEL_BEARER: Final[str] = "at1.do-not-emit-conflict-bearer"
SENTINEL_OBJECT_KEY: Final[str] = "objects/sha256/do/-not/do-not-emit-object-key"
SENTINEL_URL: Final[str] = "https://do-not-emit-presigned-url.example"
SENTINEL_DIGEST: Final[str] = "d" * 64
SENTINEL_IDEMPOTENCY_KEY: Final[str] = "e" * 8 + "-eeee-4eee-8eee-" + "e" * 12

ALL_SENTINELS: Final[tuple[str, ...]] = (
    SENTINEL_LOCATOR,
    SENTINEL_DRAFT,
    SENTINEL_CANDIDATE_BYTES.decode("utf-8"),
    SENTINEL_BEARER,
    SENTINEL_OBJECT_KEY,
    SENTINEL_URL,
    SENTINEL_DIGEST,
    SENTINEL_IDEMPOTENCY_KEY,
)

#: Member names no conflict wire model may ever declare.
FORBIDDEN_MEMBER_NAMES: Final[tuple[str, ...]] = (
    "digest",
    "sha256",
    "object_key",
    "locator",
    "url",
    "bytes",
    "content",
    "diff",
    "merged_text",
    "token",
    "credential",
    "provider",
)


@dataclass
class RecordingEventSink:
    """Capture every diagnostic event the application emits."""

    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: dict[str, object] | None = None) -> None:
        self.events.append((str(event_name), dict(fields or {})))

    def rendered(self) -> str:
        return json.dumps(self.events, default=str)


class _ReadyProbe:
    """Readiness probe stub: the conflict routes never consult it."""

    async def check(self) -> None: ...


def _sentinel_conflict() -> SourceConflict:
    return SourceConflict(
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


@dataclass
class PrivacyHarness:
    """One test client, the offline states and the recording event sink."""

    client: TestClient
    conflict_state: OfflineSourceConflictState
    sync_state: OfflineSmallFileSyncState
    sink: RecordingEventSink
    access_credential: str


def _exchange_device_credential(client: TestClient) -> str:
    created = client.post(
        "/api/auth/device-authorizations",
        headers={"Origin": ORIGIN},
        json={
            "client_instance_id": str(uuid4()),
            "device_name": "Privacy desktop",
            "platform_class": "obsidian_desktop",
            "platform_name": "windows",
            "plugin_version": "1.4.0",
            "requested_scope": "obsidian_sync",
        },
    )
    assert created.status_code == 200, created.text
    grant = dict(created.json()["data"])
    login = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
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


@pytest.fixture
def privacy_harness() -> Iterator[PrivacyHarness]:
    conflict_state = OfflineSourceConflictState(
        open_conflicts=(_sentinel_conflict(),),
        evidence_bytes=SENTINEL_CANDIDATE_BYTES,
    )
    sync_state = OfflineSmallFileSyncState()
    sink = RecordingEventSink()
    application: FastAPI = create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        event_sink=sink,
        web_authentication=compose_offline_web_authentication(
            clock=OfflineAuthenticationClock(),
            state=OfflineAuthenticationState(totp_active=False),
        ),
        small_file_sync=compose_offline_small_file_sync(state=sync_state),
        source_conflicts=compose_offline_source_conflicts(state=conflict_state),
    )
    with TestClient(application, base_url=ORIGIN) as client:
        yield PrivacyHarness(
            client=client,
            conflict_state=conflict_state,
            sync_state=sync_state,
            sink=sink,
            access_credential=_exchange_device_credential(client),
        )


def _bearer(harness: PrivacyHarness) -> dict[str, str]:
    return {"Authorization": f"Bearer {harness.access_credential}"}


def _assert_no_sentinel(*blobs: str) -> None:
    combined = "\n".join(blobs)
    for sentinel in ALL_SENTINELS:
        assert sentinel not in combined, f"sentinel leaked: {sentinel}"


# --- the typed error surface ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_diagnostics() -> Iterator[None]:
    yield
    reset_diagnostics_for_testing()


def test_typed_conflict_error_never_renders_private_causes(
    tmp_path: Path,
) -> None:
    provider_cause = RuntimeError(
        f"key={SENTINEL_OBJECT_KEY} url={SENTINEL_URL} digest={SENTINEL_DIGEST} "
        f"locator={SENTINEL_LOCATOR} token={SENTINEL_BEARER}"
    )
    try:
        raise provider_cause
    except RuntimeError as cause:
        try:
            raise SourceConflictError(
                ErrorCode.SOURCE_CONFLICT_EVIDENCE_INTEGRITY_FAILED
            ) from cause
        except SourceConflictError as captured:
            error = captured

    # The source chain carries every sentinel; otherwise the boundary test is moot.
    assert SENTINEL_OBJECT_KEY in str(error.__cause__)
    stdout, stderr = StringIO(), StringIO()
    settings_logger: DiagnosticLogger = configure_diagnostics(
        _load_test_settings(tmp_path), stdout=stdout, stderr=stderr
    )
    settings_logger.emit_application_error(error)

    for line in (*stdout.getvalue().splitlines(), *stderr.getvalue().splitlines()):
        assert isinstance(json.loads(line), dict)
    _assert_no_sentinel(stdout.getvalue(), stderr.getvalue(), str(error), repr(error))


def _load_test_settings(tmp_path: Path) -> Any:
    from personal_os.runtime_configuration.loading import load_runtime_settings
    from personal_os.runtime_configuration.models import ServiceName

    return load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_ENVIRONMENT": "test",
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
        },
    )


# --- the wire model surface -----------------------------------------------------------------------


def test_conflict_wire_models_declare_no_sensitive_members() -> None:
    from api_runtime.small_file_sync_models import (
        SmallFileConflictCaptureData,
        SmallFilePreflightData,
    )
    from api_runtime.source_conflict_models import (
        SourceConflictCandidateData,
        SourceConflictData,
        SourceConflictDetailData,
        SourceConflictPageData,
        SourceConflictResolutionData,
    )

    for model in (
        SourceConflictData,
        SourceConflictDetailData,
        SourceConflictPageData,
        SourceConflictResolutionData,
        SourceConflictCandidateData,
        SmallFilePreflightData,
        SmallFileConflictCaptureData,
    ):
        members = set(model.model_json_schema()["properties"])
        for forbidden in FORBIDDEN_MEMBER_NAMES:
            assert forbidden not in members, (model.__name__, forbidden)


def test_resolve_body_schema_refuses_every_private_value_shape() -> None:
    from api_runtime.source_conflict_models import SourceConflictResolveRequest

    schema = SourceConflictResolveRequest.model_json_schema()
    assert schema["additionalProperties"] is False
    members = set(schema["properties"])
    assert members == {
        "resolution_event_id",
        "idempotency_key",
        "resolution_kind",
        "reviewed_remote_version_id",
        "verified_candidate_object_id",
    }


# --- the route surface ----------------------------------------------------------------------------


def test_capture_and_inbox_journeys_never_emit_sentinel_evidence(
    privacy_harness: PrivacyHarness,
) -> None:
    harness = privacy_harness
    responses: list[str] = []

    # The capture lane: a local edit of a sentinel-locator note whose source
    # the server deleted, uploading sentinel candidate bytes.
    body = {
        "event_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "operation": "update",
        "local_file_id": str(uuid4()),
        "source_id": str(uuid4()),
        "base_version_id": str(uuid4()),
        "normalized_locator": SENTINEL_LOCATOR,
        "sha256": sha256(SENTINEL_CANDIDATE_BYTES).hexdigest(),
        "size_bytes": len(SENTINEL_CANDIDATE_BYTES),
        "media_type": "text/markdown",
        "policy_revision": 7,
    }
    harness.sync_state.current_reference = None
    harness.sync_state.deleted_source_ids.add(UUID(str(body["source_id"])))
    preflight = harness.client.post(
        "/api/sync/journal-events/preflight", headers=_bearer(harness), json=body
    )
    assert preflight.status_code == 200, preflight.text
    responses.append(preflight.text)
    token = str(dict(preflight.json()["data"])["operation_id"])
    captured = harness.client.put(
        f"/api/uploads/{token}/conflict-content",
        headers={**_bearer(harness), "Content-Type": "application/octet-stream"},
        content=SENTINEL_CANDIDATE_BYTES,
    )
    assert captured.status_code == 200, captured.text
    responses.append(captured.text)

    # The Inbox lanes over the seeded open conflict with sentinel evidence.
    listing = harness.client.get("/api/sync/conflicts", headers=_bearer(harness))
    assert listing.status_code == 200, listing.text
    responses.append(listing.text)
    detail = harness.client.get(f"/api/sync/conflicts/{_CONFLICT_ID}", headers=_bearer(harness))
    assert detail.status_code == 200, detail.text
    responses.append(detail.text)

    # The resolution-candidate upload with sentinel merged-draft bytes: the
    # digest is derived from the bytes (the declaration must match), while
    # the merged draft text itself is the sentinel.
    candidate = harness.client.put(
        f"/api/sync/conflicts/{_CONFLICT_ID}/candidate",
        headers={
            **_bearer(harness),
            "Content-Type": "application/octet-stream",
            "x-candidate-sha256": sha256(SENTINEL_DRAFT.encode("utf-8")).hexdigest(),
            "x-candidate-media-type": "text/markdown",
        },
        content=SENTINEL_DRAFT.encode("utf-8"),
    )
    assert candidate.status_code == 200, candidate.text
    responses.append(candidate.text)

    resolve = harness.client.post(
        f"/api/sync/conflicts/{_CONFLICT_ID}/resolve",
        headers=_bearer(harness),
        json={
            "resolution_event_id": str(uuid4()),
            "idempotency_key": str(uuid4()),
            "resolution_kind": "keep_remote",
            "reviewed_remote_version_id": str(uuid4()),
        },
    )
    assert resolve.status_code == 200, resolve.text
    responses.append(resolve.text)

    # The diagnostics the whole journey emitted stay clean.
    responses.append(harness.sink.rendered())
    _assert_no_sentinel(*responses)


def test_verified_evidence_read_carries_only_exact_content_headers(
    privacy_harness: PrivacyHarness,
) -> None:
    """The evidence stream delivers the exact bytes and nothing else about them.

    The verified read is the product: its body IS the candidate. The privacy
    contract pins the surrounding surface instead — the response headers
    carry exactly the canonical media type, the exact byte length and the
    cache posture, never a digest, object key, URL or locator.
    """

    harness = privacy_harness
    response = harness.client.get(
        f"/api/sync/conflicts/{_CONFLICT_ID}/evidence/candidate",
        headers={**_bearer(harness), "accept": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    assert response.content == SENTINEL_CANDIDATE_BYTES
    headers = {name.lower(): value for name, value in response.headers.items()}
    assert headers["content-type"] == "text/markdown"
    assert headers["content-length"] == str(len(SENTINEL_CANDIDATE_BYTES))
    assert "no-store" in headers["cache-control"]
    for sentinel in (
        SENTINEL_OBJECT_KEY,
        SENTINEL_URL,
        SENTINEL_LOCATOR,
        SENTINEL_BEARER,
        SENTINEL_DIGEST,
    ):
        assert sentinel not in response.headers.get("x-content-sha256", "")
        rendered_headers = "\n".join(f"{k}:{v}" for k, v in headers.items())
        assert sentinel not in rendered_headers


def test_error_envelope_of_every_typed_conflict_failure_stays_closed(
    privacy_harness: PrivacyHarness,
) -> None:
    harness = privacy_harness
    unknown_conflict = str(uuid4())
    responses = [
        harness.client.get(
            f"/api/sync/conflicts/{unknown_conflict}", headers=_bearer(harness)
        ).text,
        harness.client.put(
            f"/api/sync/conflicts/{unknown_conflict}/candidate",
            headers={
                **_bearer(harness),
                "x-candidate-sha256": sha256(b"mismatching").hexdigest(),
                "x-candidate-media-type": "text/markdown",
                "Content-Type": "application/octet-stream",
            },
            content=b"mismatching",
        ).text,
    ]
    _assert_no_sentinel(*responses, harness.sink.rendered())


# --- replay determinism of the frozen read model --------------------------------------------------


def test_resolution_result_rendering_carries_only_opaque_outcomes() -> None:
    from api_runtime.source_conflict_models import source_conflict_resolution_data

    from personal_os.source_conflicts.contracts import ConflictResolutionKind

    result = ConflictResolutionResult(
        kind=ConflictResolutionOutcome.STALE_SUCCESSOR,
        conflict_id=_CONFLICT_ID,
        resolution_event_id=uuid4(),
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
        resulting_version_id=None,
        successor=_sentinel_conflict(),
        completed_at=_CAPTURED_AT,
    )
    rendered = source_conflict_resolution_data(result).model_dump_json()
    _assert_no_sentinel(rendered)
    assert '"successor_conflict_id"' in rendered
