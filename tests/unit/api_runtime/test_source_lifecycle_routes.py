"""Source lifecycle route factory: bearer auth, OBSIDIAN_SYNC scope and field rejection.

These tests drive the endpoint factory through a stand-in runtime dependency:
the bearer credential resolves through the same ``authenticate_access``
service the offline graph uses, so a missing header, a non-Bearer scheme or
a session cookie closes the request with the closed invalid-credential code.
A wrong scope (any other ``DeviceScope`` token than ``OBSIDIAN_SYNC``) is the
canonical scope-denied rejection, a body carrying ``workspace_id`` or
``device_id`` is the canonical validation rejection and a typed domain error
maps to its registered HTTP status with the ``Cache-Control: no-store``
posture the lifecycle surface carries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from api_runtime.application import create_api_application
from api_runtime.authentication_composition import (
    OFFLINE_WEB_ALLOWED_ORIGIN,
    compose_offline_web_authentication,
)
from api_runtime.source_lifecycle_composition import (
    SourceLifecycleRuntime,
)
from fastapi.testclient import TestClient

from personal_os.authentication.contracts import AuthenticatedDeviceContext, DeviceScope
from personal_os.authentication.device_tokens import AuthenticatedAccessToken
from personal_os.authentication.errors import AuthenticationError
from personal_os.error_contracts.codes import ErrorCode
from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.source_lifecycle.commands import (
    LifecycleOperation,
    LifecycleState,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.errors import SourceLifecycleError, SourceLifecycleErrorCode
from personal_os.source_lifecycle.metrics import InMemorySourceLifecycleMetrics
from personal_os.source_lifecycle.ports import (
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
)
from personal_os.source_lifecycle.service import SourceLifecycleService
from personal_os.source_locators import NormalizedLocator

ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN
WORKSPACE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000002")
DEVICE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000003")
USER_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000004")
SOURCE_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000010")
EVENT_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000011")
EXPECTED_VERSION_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000013")
TOMBSTONE_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000014")


def _sync_token(unique: str | None = None) -> str:
    """Render an opaque at1-like token unique per test invocation."""

    suffix = unique if unique is not None else uuid4().hex
    return f"at1.test.{suffix}"


def _device_context(scope: DeviceScope) -> AuthenticatedDeviceContext:
    return AuthenticatedDeviceContext(
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        device_id=DEVICE_ID,
        scope=scope,
    )


def _build_token(scope: DeviceScope, unique: str | None = None) -> AuthenticatedAccessToken:
    return AuthenticatedAccessToken(
        context=_device_context(scope),
        database_now=datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )


def _rename_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "event_id": str(EVENT_ID),
        "idempotency_key": "lifecycle-rename-001",
        "source_id": str(SOURCE_ID),
        "operation": LifecycleOperation.RENAME.value,
        "expected_version_id": str(EXPECTED_VERSION_ID),
        "expected_locator": "notes/old.md",
        "target_locator": "notes/new.md",
        "tombstone_id": None,
        "policy_revision": 1,
        "client_timestamp": "2026-08-20T01:02:03Z",
    }
    body.update(overrides)
    return body


def _build_commit_result() -> SourceLifecycleCommitResult:
    """Build a stable rename commit result the offline store returns."""

    return SourceLifecycleCommitResult(
        source_id=SOURCE_ID,
        source_version_id=UUID("018f47a0-7b00-7000-8000-000000000099"),
        event_id=EVENT_ID,
        event_sequence=1,
        state=LifecycleState.ACTIVE,
        tombstone_id=None,
        resulting_locator=NormalizedLocator("notes/new.md"),
        committed_at=datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )


@dataclass(frozen=True, slots=True)
class _StubStore:
    """One-call store double honouring the deterministic offline stub semantics."""

    committed: SourceLifecycleCommitResult
    error: SourceLifecycleError | None = None

    async def resolve_committed(
        self,
        command: object,
        device_context: LifecycleDeviceContext,
        request_fingerprint: object,
        diagnostic_context: object,
    ) -> SourceLifecycleCommitResult | None:
        del command, device_context, request_fingerprint, diagnostic_context
        if self.error is not None:
            raise self.error
        return None

    async def commit(
        self,
        command: object,
        device_context: LifecycleDeviceContext,
        request_fingerprint: object,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: object,
    ) -> SourceLifecycleCommitResult:
        del command, device_context, request_fingerprint, diagnostic_context
        if self.error is not None:
            raise self.error
        return self.committed


@dataclass(frozen=True, slots=True)
class _StubPolicy:
    decision: LifecyclePolicyDecision

    async def evaluate_lifecycle(
        self,
        command: object,
        device_context: LifecycleDeviceContext,
    ) -> LifecyclePolicyDecision:
        del command, device_context
        return self.decision


@dataclass(frozen=True, slots=True)
class _RuntimeHarness:
    """A composed runtime plus the credential the test must present."""

    runtime: SourceLifecycleRuntime
    credential: str
    unknown_credential: str = field(default_factory=lambda: _sync_token("unknown"))


def _web_auth_with_token(credential: str, scope: DeviceScope) -> object:
    """Build a web-authentication double recognising one access credential."""

    web_authentication = compose_offline_web_authentication()

    async def _authenticate_access(access_credential: str) -> AuthenticatedAccessToken:
        if access_credential == credential:
            return _build_token(scope)
        raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)

    web_authentication.device_token_service.authenticate_access = _authenticate_access  # type: ignore[method-assign]
    return web_authentication


def _harness(
    *,
    store: _StubStore | None = None,
    unique: str | None = None,
) -> _RuntimeHarness:
    credential = _sync_token(unique)
    web_authentication = _web_auth_with_token(credential, DeviceScope.OBSIDIAN_SYNC)
    decision = LifecyclePolicyDecision(
        workspace_id=WORKSPACE_ID,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        policy_revision_number=1,
        subject=object(),  # type: ignore[arg-type]
        expected_locator=NormalizedLocator("notes/old.md"),
        target_locator=NormalizedLocator("notes/new.md"),
    )
    store = store or _StubStore(committed=_build_commit_result())
    policy = _StubPolicy(decision=decision)
    metrics = InMemorySourceLifecycleMetrics()
    service = SourceLifecycleService(
        store=store,  # type: ignore[arg-type]
        policy=policy,  # type: ignore[arg-type]
        metrics=metrics,
    )
    runtime = SourceLifecycleRuntime(
        service=service,
        store=store,  # type: ignore[arg-type]
        policy=policy,  # type: ignore[arg-type]
        metrics=metrics,
        lifecycle_diagnostics=metrics,
        web_authentication=web_authentication,  # type: ignore[arg-type]
    )
    return _RuntimeHarness(runtime=runtime, credential=credential)


class _ReadyProbe:
    """Readiness probe stub: the route handler never consults dependencies."""

    async def check(self) -> None: ...


def _build_client(harness: _RuntimeHarness) -> TestClient:
    application = create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=harness.runtime.web_authentication,
        source_lifecycle=harness.runtime,
    )
    return TestClient(application, base_url="http://test")


def _bearer(harness: _RuntimeHarness) -> dict[str, str]:
    return {"Authorization": f"Bearer {harness.credential}"}


def test_missing_authorization_header_returns_device_credential_invalid() -> None:
    harness = _harness()
    response = _build_client(harness).post("/api/sources/lifecycle-events", json=_rename_body())
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "device_credential_invalid"
    assert response.headers["cache-control"] == "no-store"


def test_non_bearer_scheme_returns_device_credential_invalid() -> None:
    harness = _harness()
    response = _build_client(harness).post(
        "/api/sources/lifecycle-events",
        json=_rename_body(),
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "device_credential_invalid"


def test_session_cookie_is_ignored_as_an_authority() -> None:
    harness = _harness()
    response = _build_client(harness).post(
        "/api/sources/lifecycle-events",
        json=_rename_body(),
        headers={
            "Cookie": "__Host-admin_session=anything",
            "Origin": ORIGIN,
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "device_credential_invalid"


def test_unknown_at1_returns_device_credential_invalid() -> None:
    harness = _harness()
    response = _build_client(harness).post(
        "/api/sources/lifecycle-events",
        json=_rename_body(),
        headers={"Authorization": f"Bearer {harness.unknown_credential}"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "device_credential_invalid"
    assert response.headers["cache-control"] == "no-store"


def test_obsidian_sync_scope_succeeds_end_to_end() -> None:
    """The OBSIDIAN_SYNC scope succeeds end-to-end: the canonical happy path."""

    harness = _harness()
    response = _build_client(harness).post(
        "/api/sources/lifecycle-events",
        json=_rename_body(),
        headers=_bearer(harness),
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"]["state"] == "active"


def test_body_carrying_workspace_id_is_rejected() -> None:
    harness = _harness()
    response = _build_client(harness).post(
        "/api/sources/lifecycle-events",
        json=_rename_body(workspace_id=str(WORKSPACE_ID)),
        headers=_bearer(harness),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"


def test_body_carrying_device_id_is_rejected() -> None:
    harness = _harness()
    response = _build_client(harness).post(
        "/api/sources/lifecycle-events",
        json=_rename_body(device_id=str(DEVICE_ID)),
        headers=_bearer(harness),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"


def test_locator_conflict_maps_to_409() -> None:
    store = _StubStore(
        committed=_build_commit_result(),
        error=SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT),
    )
    harness = _harness(store=store)
    response = _build_client(harness).post(
        "/api/sources/lifecycle-events",
        json=_rename_body(),
        headers=_bearer(harness),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "source_locator_conflict"
    assert response.headers["cache-control"] == "no-store"


def test_tombstone_not_found_maps_to_404() -> None:
    store = _StubStore(
        committed=_build_commit_result(),
        error=SourceLifecycleError(SourceLifecycleErrorCode.TOMBSTONE_NOT_FOUND),
    )
    harness = _harness(store=store)
    response = _build_client(harness).post(
        "/api/sources/lifecycle-events",
        json={
            "event_id": str(EVENT_ID),
            "idempotency_key": "lifecycle-restore-001",
            "source_id": str(SOURCE_ID),
            "operation": LifecycleOperation.RESTORE.value,
            "expected_version_id": str(EXPECTED_VERSION_ID),
            "expected_locator": None,
            "target_locator": "notes/restored.md",
            "tombstone_id": str(TOMBSTONE_ID),
            "policy_revision": 1,
            "client_timestamp": None,
        },
        headers=_bearer(harness),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "source_tombstone_not_found"


def test_commit_outcome_unknown_maps_to_503() -> None:
    store = _StubStore(
        committed=_build_commit_result(),
        error=SourceLifecycleError(SourceLifecycleErrorCode.COMMIT_OUTCOME_UNKNOWN),
    )
    harness = _harness(store=store)
    response = _build_client(harness).post(
        "/api/sources/lifecycle-events",
        json=_rename_body(),
        headers=_bearer(harness),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "source_lifecycle_commit_outcome_unknown"


def test_successful_commit_is_200_and_carries_no_store() -> None:
    harness = _harness()
    response = _build_client(harness).post(
        "/api/sources/lifecycle-events",
        json=_rename_body(),
        headers=_bearer(harness),
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["state"] == "active"
    assert data["resulting_locator"] == "notes/new.md"
