"""Serve composition of the small-file sync runtime: the real adapter graph.

These tests prove the serve composition binds the production adapters — the
durable PostgreSQL upload-operation store, the real R2 object store with its
lazy per-process client, the real policy enforcement service behind the
locator-aware small-file guard, the durable source-publication store and the
canonical read store — and never an offline double. No database connection,
R2 request or socket is opened: the adapters only capture the engine, the
client source and the verifier at construction, which is exactly what lets
the serve process compose the graph before its first request.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest
from api_runtime.exclusion_policy_crypto import Ed25519PolicySigner
from api_runtime.small_file_sync_composition import (
    OfflineSmallFileSyncState,
    PolicyEnforcementSmallFileGuard,
    SmallFileSyncRuntime,
    compose_offline_small_file_sync,
    compose_small_file_sync,
)
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from personal_os.diagnostics.context import (
    DiagnosticContext,
    create_diagnostic_context,
)
from personal_os.diagnostics.logging import DiagnosticLogger
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.exclusion_policy.enforcement import (
    PolicyBoundary,
    PolicyDecision,
    PolicyEnforcementService,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.object_storage import CanonicalMediaType, ContentDigest
from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.small_file_sync.contracts import (
    NormalizedLocator,
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
)
from personal_os.sources.publication import SourceVersionPublicationService
from postgresql_source_store.canonical_read import PostgresqlCanonicalSourceReadStore
from postgresql_source_store.publication_store import PostgresqlSourcePublicationStore
from postgresql_source_store.small_file_sync_operations import (
    PostgresqlSmallFileUploadOperationStore,
)
from r2_object_storage.adapter import R2S3ObjectStore
from r2_object_storage.settings import LoadedR2Credentials, ObjectStorageSettings

_R2_ENDPOINT: Final[str] = f"https://{'0' * 32}.r2.cloudflarestorage.com"
_SIGNER_SEED: Final[bytes] = bytes(range(32))


@pytest.fixture
def serve_engine(tmp_path: Path) -> Iterator[AsyncEngine]:
    engine = create_async_engine("postgresql+psycopg://user:pass@127.0.0.1:5432/db")
    try:
        yield engine
    finally:
        engine.sync_engine.dispose()


@pytest.fixture
def serve_runtime(tmp_path: Path, serve_engine: AsyncEngine) -> SmallFileSyncRuntime:
    spool_root = tmp_path / "spool"
    spool_root.mkdir()
    settings = ObjectStorageSettings(
        environment=RuntimeEnvironment.LOCAL,
        secret_root=tmp_path,
        r2_endpoint=_R2_ENDPOINT,
        r2_bucket_name="personal-knowledge-objects",
        r2_access_key_id_file="r2_access_key_id",
        r2_secret_access_key_file="r2_secret_access_key",
        object_storage_spool_root=spool_root,
    )
    credentials = LoadedR2Credentials(
        access_key_id=SecretStr("access-key-id"),
        secret_access_key=SecretStr("secret-access-key"),
    )
    return compose_small_file_sync(
        engine=serve_engine,
        signer=Ed25519PolicySigner.from_seed_bytes(_SIGNER_SEED),
        object_storage_settings=settings,
        object_storage_credentials=credentials,
        logger=DiagnosticLogger({"service": "api", "environment": "local"}),
    )


def test_serve_composition_binds_the_real_adapters(serve_runtime: SmallFileSyncRuntime) -> None:
    service = serve_runtime.service
    assert isinstance(service.operation_store, PostgresqlSmallFileUploadOperationStore)
    assert isinstance(service.object_store, R2S3ObjectStore)
    assert isinstance(service.current_sources, PostgresqlCanonicalSourceReadStore)
    assert isinstance(service.publication_service, SourceVersionPublicationService)
    assert isinstance(service.publication_service.store, PostgresqlSourcePublicationStore)
    assert isinstance(service.publication_service.policy_guard, PolicyEnforcementService)
    assert isinstance(service.policy_guard, PolicyEnforcementSmallFileGuard)
    assert isinstance(service.policy_guard.enforcement, PolicyEnforcementService)


def test_serve_composition_never_binds_an_offline_double(
    serve_runtime: SmallFileSyncRuntime,
) -> None:
    offline = compose_offline_small_file_sync(state=OfflineSmallFileSyncState())
    offline_types = {type(offline.service.operation_store), type(offline.service.object_store)}
    serve_types = {
        type(serve_runtime.service.operation_store),
        type(serve_runtime.service.object_store),
    }
    assert serve_types.isdisjoint(offline_types)


def test_serve_runtime_serves_the_sync_routes(
    serve_runtime: SmallFileSyncRuntime,
) -> None:
    """The composed serve runtime plugs straight into the application factory."""

    from api_runtime.application import create_api_application
    from api_runtime.authentication_composition import compose_offline_web_authentication
    from fastapi import FastAPI

    class _ReadyProbe:
        async def check(self) -> None: ...

    application: FastAPI = create_api_application(
        environment=RuntimeEnvironment.LOCAL,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(),
        small_file_sync=serve_runtime,
    )
    paths = {route.path for route in application.routes}
    assert "/api/sync/journal-events/preflight" in paths
    assert "/api/uploads/{operation_id}/content" in paths


# --- the locator-aware guard adapter ----------------------------------------------------------


@dataclass
class _RecordingEnforcement:
    """Enforcement double recording the one evaluation it is asked for."""

    subject: PolicySubject | None = None
    boundary: PolicyBoundary | None = None

    async def authorize_preflight(
        self,
        *,
        subject: PolicySubject,
        boundary: PolicyBoundary,
        context: DiagnosticContext,
    ) -> PolicyDecision | None:
        del context
        self.subject = subject
        self.boundary = boundary
        return None


def _build_create_preflight() -> SmallFilePreflight:
    return SmallFilePreflight(
        event_id=uuid4(),
        idempotency_key=SmallFileIdempotencyKey(str(uuid4())),
        operation=SmallFileOperation.CREATE,
        local_file_id=uuid4(),
        source_id=None,
        base_version_id=None,
        normalized_locator=NormalizedLocator("notes/synced-note.md"),
        sha256=ContentDigest.parse("0" * 64),
        size_bytes=128,
        media_type=CanonicalMediaType.parse("text/markdown"),
        policy_revision_number=7,
    )


@pytest.mark.asyncio
async def test_locator_guard_evaluates_the_capture_subject_at_the_upload_boundary() -> None:
    enforcement = _RecordingEnforcement()
    guard = PolicyEnforcementSmallFileGuard(enforcement=enforcement)  # type: ignore[arg-type]
    device_context = SmallFileDeviceContext(device_id=uuid4(), workspace_id=uuid4())

    await guard.authorize_small_file(
        _build_create_preflight(), device_context, create_diagnostic_context().context
    )

    assert enforcement.boundary is PolicyBoundary.SINGLE_PART_UPLOAD
    subject = enforcement.subject
    assert subject is not None
    assert subject.workspace_id == device_context.workspace_id
    assert subject.normalized_locator == "notes/synced-note.md"
    assert subject.media_type == CanonicalMediaType.parse("text/markdown")
    assert subject.size_bytes == 128
    assert subject.source_id is None  # a create carries no canonical source yet


@pytest.mark.asyncio
async def test_locator_guard_carries_the_update_source_identity() -> None:
    enforcement = _RecordingEnforcement()
    guard = PolicyEnforcementSmallFileGuard(enforcement=enforcement)  # type: ignore[arg-type]
    device_context = SmallFileDeviceContext(device_id=uuid4(), workspace_id=uuid4())
    source_id = uuid4()
    preflight = SmallFilePreflight(
        event_id=uuid4(),
        idempotency_key=SmallFileIdempotencyKey(str(uuid4())),
        operation=SmallFileOperation.UPDATE,
        local_file_id=uuid4(),
        source_id=source_id,
        base_version_id=uuid4(),
        normalized_locator=NormalizedLocator("notes/synced-note.md"),
        sha256=ContentDigest.parse("0" * 64),
        size_bytes=128,
        media_type=CanonicalMediaType.parse("text/markdown"),
        policy_revision_number=7,
    )

    await guard.authorize_small_file(preflight, device_context, create_diagnostic_context().context)

    subject = enforcement.subject
    assert subject is not None
    assert subject.source_id == source_id


@pytest.mark.asyncio
async def test_locator_guard_propagates_the_typed_denial() -> None:
    class _DenyingEnforcement(_RecordingEnforcement):
        async def authorize_preflight(
            self,
            *,
            subject: PolicySubject,
            boundary: PolicyBoundary,
            context: DiagnosticContext,
        ) -> PolicyDecision | None:
            del subject, boundary, context
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)

    guard = PolicyEnforcementSmallFileGuard(enforcement=_DenyingEnforcement())  # type: ignore[arg-type]
    device_context = SmallFileDeviceContext(device_id=uuid4(), workspace_id=uuid4())

    with pytest.raises(ExclusionPolicyError):
        await guard.authorize_small_file(
            _build_create_preflight(), device_context, create_diagnostic_context().context
        )
