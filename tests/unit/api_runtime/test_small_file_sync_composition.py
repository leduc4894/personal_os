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

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import pytest
from api_runtime.exclusion_policy_crypto import Ed25519PolicySigner
from api_runtime.small_file_sync_composition import (
    BoundPolicySmallFilePublicationGateway,
    OfflineSmallFileClock,
    OfflineSmallFileSyncState,
    OfflineSmallFileUploadOperationStore,
    PolicyEnforcementSmallFileGuard,
    SmallFileSyncRuntime,
    compose_offline_small_file_sync,
    compose_small_file_sync,
)
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tests.unit.small_file_sync.fakes import (
    SYNC_CONTENT_BYTES,
    SYNC_CONTENT_DIGEST,
    SYNC_MEDIA_TYPE,
    CallLedger,
    FakeCanonicalObjectStore,
    FakeSourcePublicationStore,
    FixedUtcClock,
    ProbedByteStream,
)

from personal_os.diagnostics.context import (
    DiagnosticContext,
    create_diagnostic_context,
)
from personal_os.diagnostics.logging import DiagnosticLogger
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    ExclusionPolicyRevision,
    PolicySubject,
    RawPolicyDecision,
)
from personal_os.exclusion_policy.enforcement import (
    ActivePolicySnapshotMaterial,
    AllowedPolicyRevisionBinding,
    PolicyBoundary,
    PolicyDecision,
    PolicyEnforcementService,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.metrics import (
    EvaluationMetricOutcome,
    InMemoryExclusionPolicyMetrics,
)
from personal_os.exclusion_policy.signatures import (
    SNAPSHOT_SIGNING_DOMAIN,
    build_signed_message,
    build_snapshot_payload,
    compute_payload_sha256_hex,
)
from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.small_file_sync.contracts import (
    NormalizedLocator,
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
)
from personal_os.small_file_sync.errors import SmallFileSyncError
from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    SourceTitle,
    SourceType,
)
from personal_os.sources.metrics import InMemorySourcePublicationMetrics
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
    return _compose_serve_runtime(
        tmp_path=tmp_path,
        serve_engine=serve_engine,
        signer=Ed25519PolicySigner.from_seed_bytes(_SIGNER_SEED),
    )


def _compose_serve_runtime(
    *,
    tmp_path: Path,
    serve_engine: AsyncEngine,
    signer: Ed25519PolicySigner,
) -> SmallFileSyncRuntime:
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
        signer=signer,
        object_storage_settings=settings,
        object_storage_credentials=credentials,
        logger=DiagnosticLogger({"service": "api", "environment": "local"}),
    )


def test_serve_composition_binds_the_bound_policy_publication_gateway(
    serve_runtime: SmallFileSyncRuntime,
) -> None:
    service = serve_runtime.service
    assert isinstance(service.operation_store, PostgresqlSmallFileUploadOperationStore)
    assert isinstance(service.object_store, R2S3ObjectStore)
    assert isinstance(service.current_sources, PostgresqlCanonicalSourceReadStore)
    assert isinstance(service.publication_gateway, BoundPolicySmallFilePublicationGateway)
    assert isinstance(service.publication_gateway.store, PostgresqlSourcePublicationStore)
    assert service.publication_gateway.operation_store is service.operation_store
    assert isinstance(service.publication_gateway.enforcement, PolicyEnforcementService)
    assert isinstance(service.policy_guard, PolicyEnforcementSmallFileGuard)
    assert isinstance(service.policy_guard.enforcement, PolicyEnforcementService)


def test_serve_composition_binds_the_shared_policy_metrics_sink_into_enforcement(
    tmp_path: Path,
    serve_engine: AsyncEngine,
) -> None:
    """The serve composition records policy evaluations into the bound sink.

    Spec 2026-08-24 C2: the small-file composition must hand its shared
    exclusion-policy metrics sink to the enforcement service, so the
    ``exclusion_policy_evaluation_total`` counters actually record in the
    serve graph instead of silently recording nothing.
    """

    recorder = InMemoryExclusionPolicyMetrics()
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
    runtime = compose_small_file_sync(
        engine=serve_engine,
        signer=Ed25519PolicySigner.from_seed_bytes(_SIGNER_SEED),
        object_storage_settings=settings,
        object_storage_credentials=LoadedR2Credentials(
            access_key_id=SecretStr("access-key-id"),
            secret_access_key=SecretStr("secret-access-key"),
        ),
        logger=DiagnosticLogger({"service": "api", "environment": "local"}),
        policy_metrics=recorder,
    )
    workspace_id = uuid4()
    revision = ExclusionPolicyRevision(
        policy_revision_id=uuid4(),
        workspace_id=workspace_id,
        revision_number=1,
    )
    payload_bytes = build_snapshot_payload(
        revision,
        parent_policy_revision_id=None,
        published_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    signer = Ed25519PolicySigner.from_seed_bytes(_SIGNER_SEED)
    material = ActivePolicySnapshotMaterial(
        workspace_id=workspace_id,
        policy_revision_id=revision.policy_revision_id,
        revision_number=revision.revision_number,
        payload_bytes=payload_bytes,
        payload_sha256=compute_payload_sha256_hex(payload_bytes),
        signature_bytes=signer.sign(build_signed_message(SNAPSHOT_SIGNING_DOMAIN, payload_bytes)),
        public_key_bytes=signer.public_key_bytes,
    )
    guard = runtime.service.policy_guard
    assert isinstance(guard, PolicyEnforcementSmallFileGuard)

    guard.enforcement.evaluate_material(
        material,
        subject=PolicySubject(
            workspace_id=workspace_id,
            normalized_locator="notes/allowed.md",
            media_type=CanonicalMediaType.parse("text/markdown"),
            size_bytes=128,
        ),
        boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
    )

    snapshot = recorder.policy_diagnostics()
    assert dict(snapshot.evaluation_counters) == {
        (PolicyBoundary.SINGLE_PART_UPLOAD, EvaluationMetricOutcome.ALLOWED): 1
    }


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


def test_serve_composition_verifies_an_active_snapshot_signed_before_rotation(
    tmp_path: Path,
    serve_engine: AsyncEngine,
) -> None:
    old_signer = Ed25519PolicySigner.from_seed_bytes(bytes(range(32, 64)))
    current_signer = Ed25519PolicySigner.from_seed_bytes(_SIGNER_SEED)
    runtime = _compose_serve_runtime(
        tmp_path=tmp_path,
        serve_engine=serve_engine,
        signer=current_signer,
    )
    workspace_id = uuid4()
    revision = ExclusionPolicyRevision(
        policy_revision_id=uuid4(),
        workspace_id=workspace_id,
        revision_number=1,
    )
    payload_bytes = build_snapshot_payload(
        revision,
        parent_policy_revision_id=None,
        published_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    material = ActivePolicySnapshotMaterial(
        workspace_id=workspace_id,
        policy_revision_id=revision.policy_revision_id,
        revision_number=revision.revision_number,
        payload_bytes=payload_bytes,
        payload_sha256=compute_payload_sha256_hex(payload_bytes),
        signature_bytes=old_signer.sign(
            build_signed_message(SNAPSHOT_SIGNING_DOMAIN, payload_bytes)
        ),
        public_key_bytes=old_signer.public_key_bytes,
    )
    guard = runtime.service.policy_guard
    assert isinstance(guard, PolicyEnforcementSmallFileGuard)

    decision = guard.enforcement.evaluate_material(
        material,
        subject=PolicySubject(
            workspace_id=workspace_id,
            normalized_locator="notes/allowed.md",
            media_type=CanonicalMediaType.parse("text/markdown"),
            size_bytes=128,
        ),
        boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
    )

    assert decision.enforced_decision is EnforcedPolicyDecision.ALLOWED


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
    active_revision_number: int = 1

    async def authorize_preflight(
        self,
        *,
        subject: PolicySubject,
        boundary: PolicyBoundary,
        context: DiagnosticContext,
    ) -> PolicyDecision:
        del context
        self.subject = subject
        self.boundary = boundary
        return PolicyDecision(
            workspace_id=subject.workspace_id,
            policy_revision_id=uuid4(),
            revision_number=self.active_revision_number,
            subject_fingerprint=bytes(32),
            raw_decision=RawPolicyDecision.ALLOWED,
            enforced_decision=EnforcedPolicyDecision.ALLOWED,
            matched_rule_ids=(),
            missing_fields=(),
            evaluated_at=datetime(2026, 8, 19, tzinfo=UTC),
        )


@dataclass
class _BoundPublicationRecordingEnforcement:
    bindings: list[AllowedPolicyRevisionBinding]
    entered_by_revision: dict[int, asyncio.Event]
    release_by_revision: dict[int, asyncio.Event]

    async def authorize_bound_publication(
        self,
        command: CreateSourceVersion,
        binding: AllowedPolicyRevisionBinding,
        diagnostic_context: DiagnosticContext,
    ) -> AllowedPolicyRevisionBinding:
        del command, diagnostic_context
        self.bindings.append(binding)
        entered = self.entered_by_revision.get(binding.policy_revision_number)
        if entered is not None:
            entered.set()
        release = self.release_by_revision.get(binding.policy_revision_number)
        if release is not None:
            await release.wait()
        return binding


def _build_gateway_create_command(workspace_id: UUID) -> CreateSourceVersion:
    return CreateSourceVersion(
        workspace_id=workspace_id,
        source_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey(str(uuid4())),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Markdown file"),
        actor=SourceActor(actor_kind=ActorKind.DEVICE, actor_id=uuid4()),
        expected_object=ExpectedObject(
            content_digest=SYNC_CONTENT_DIGEST,
            size_bytes=len(SYNC_CONTENT_BYTES),
            media_type=SYNC_MEDIA_TYPE,
        ),
        client_timestamp=None,
    )


def _build_bound_gateway(
    enforcement: _BoundPublicationRecordingEnforcement,
) -> BoundPolicySmallFilePublicationGateway:
    clock = FixedUtcClock(datetime(2026, 8, 19, tzinfo=UTC))
    ledger = CallLedger()
    return BoundPolicySmallFilePublicationGateway(
        store=FakeSourcePublicationStore(ledger=ledger),
        object_store=FakeCanonicalObjectStore(ledger=ledger, clock=clock),
        metrics=InMemorySourcePublicationMetrics(),
        clock=clock,
        enforcement=enforcement,
    )


@pytest.mark.asyncio
async def test_gateway_builds_a_fresh_immutable_guard_for_each_invocation() -> None:
    workspace_id = uuid4()
    enforcement = _BoundPublicationRecordingEnforcement(
        bindings=[], entered_by_revision={}, release_by_revision={}
    )
    gateway = _build_bound_gateway(enforcement)

    await gateway.publish_create(
        command=_build_gateway_create_command(workspace_id),
        stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
        policy_binding=AllowedPolicyRevisionBinding(
            workspace_id=workspace_id, policy_revision_number=11
        ),
        diagnostic_context=create_diagnostic_context().context,
    )
    await gateway.publish_create(
        command=_build_gateway_create_command(workspace_id),
        stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
        policy_binding=AllowedPolicyRevisionBinding(
            workspace_id=workspace_id, policy_revision_number=12
        ),
        diagnostic_context=create_diagnostic_context().context,
    )

    assert enforcement.bindings == [
        AllowedPolicyRevisionBinding(workspace_id=workspace_id, policy_revision_number=11),
        AllowedPolicyRevisionBinding(workspace_id=workspace_id, policy_revision_number=12),
    ]


@pytest.mark.asyncio
async def test_concurrent_gateway_calls_do_not_share_bound_evidence() -> None:
    workspace_id = uuid4()
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    first_release = asyncio.Event()
    second_release = asyncio.Event()
    enforcement = _BoundPublicationRecordingEnforcement(
        bindings=[],
        entered_by_revision={11: first_entered, 12: second_entered},
        release_by_revision={11: first_release, 12: second_release},
    )
    gateway = _build_bound_gateway(enforcement)
    first_task = asyncio.create_task(
        gateway.publish_create(
            command=_build_gateway_create_command(workspace_id),
            stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
            policy_binding=AllowedPolicyRevisionBinding(
                workspace_id=workspace_id, policy_revision_number=11
            ),
            diagnostic_context=create_diagnostic_context().context,
        )
    )
    second_task = asyncio.create_task(
        gateway.publish_create(
            command=_build_gateway_create_command(workspace_id),
            stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
            policy_binding=AllowedPolicyRevisionBinding(
                workspace_id=workspace_id, policy_revision_number=12
            ),
            diagnostic_context=create_diagnostic_context().context,
        )
    )
    await first_entered.wait()
    await second_entered.wait()

    second_release.set()
    second_result = await second_task
    first_release.set()
    first_result = await first_task

    assert {binding.policy_revision_number for binding in enforcement.bindings} == {11, 12}
    assert first_result.outcome.value == "published"
    assert second_result.outcome.value == "published"


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
async def test_offline_store_unbound_terminal_write_cannot_bypass_a_claimed_receive() -> None:
    state = OfflineSmallFileSyncState(now=datetime(2026, 8, 19, tzinfo=UTC))
    clock = OfflineSmallFileClock(state)
    store = OfflineSmallFileUploadOperationStore(state, clock)
    device_context = SmallFileDeviceContext(device_id=uuid4(), workspace_id=uuid4())
    preflight = _build_create_preflight()
    operation = await store.reserve_operation(
        preflight,
        device_context,
        AllowedPolicyRevisionBinding(
            workspace_id=device_context.workspace_id,
            policy_revision_number=7,
        ),
        create_diagnostic_context().context,
    )
    bound = await store.resolve_bound_operation(
        operation.operation_token,
        device_context,
        create_diagnostic_context().context,
    )

    with pytest.raises(SmallFileSyncError) as raised:
        await store.record_terminal_result(
            operation,
            SmallFileTerminalResult(
                result_kind=SmallFileTerminalResultKind.COMMITTED,
                source_id=uuid4(),
                source_version_id=uuid4(),
                content_version=1,
                committed_at=clock(),
            ),
            create_diagnostic_context().context,
        )

    assert raised.value.error_code is ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID
    await store.record_bound_terminal_result(
        bound,
        SmallFileTerminalResult(
            result_kind=SmallFileTerminalResultKind.COMMITTED,
            source_id=uuid4(),
            source_version_id=uuid4(),
            content_version=1,
            committed_at=clock(),
        ),
        create_diagnostic_context().context,
    )


@pytest.mark.asyncio
async def test_offline_store_bound_terminalization_rejects_policy_revision_drift() -> None:
    state = OfflineSmallFileSyncState(now=datetime(2026, 8, 19, tzinfo=UTC))
    clock = OfflineSmallFileClock(state)
    store = OfflineSmallFileUploadOperationStore(state, clock)
    device_context = SmallFileDeviceContext(device_id=uuid4(), workspace_id=uuid4())
    preflight = _build_create_preflight()
    operation = await store.reserve_operation(
        preflight,
        device_context,
        AllowedPolicyRevisionBinding(
            workspace_id=device_context.workspace_id,
            policy_revision_number=7,
        ),
        create_diagnostic_context().context,
    )
    bound = await store.resolve_bound_operation(
        operation.operation_token,
        device_context,
        create_diagnostic_context().context,
    )
    result = SmallFileTerminalResult(
        result_kind=SmallFileTerminalResultKind.COMMITTED,
        source_id=uuid4(),
        source_version_id=uuid4(),
        content_version=1,
        committed_at=clock(),
    )

    with pytest.raises(SmallFileSyncError) as raised:
        await store.record_bound_terminal_result(
            replace(bound, policy_revision_number=bound.policy_revision_number + 1),
            result,
            create_diagnostic_context().context,
        )

    assert raised.value.error_code is ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH
    resumed = await store.resolve_bound_operation(
        operation.operation_token,
        device_context,
        create_diagnostic_context().context,
    )
    assert resumed.terminal_result is None


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
async def test_locator_guard_returns_the_server_verified_revision_not_the_plugin_claim() -> None:
    active_revision_number = 11
    enforcement = _RecordingEnforcement(active_revision_number=active_revision_number)
    guard = PolicyEnforcementSmallFileGuard(enforcement=enforcement)  # type: ignore[arg-type]
    device_context = SmallFileDeviceContext(device_id=uuid4(), workspace_id=uuid4())
    preflight = _build_create_preflight()

    binding = await guard.authorize_small_file(
        preflight, device_context, create_diagnostic_context().context
    )

    assert binding == AllowedPolicyRevisionBinding(
        workspace_id=device_context.workspace_id,
        policy_revision_number=active_revision_number,
    )
    assert binding.policy_revision_number != preflight.policy_revision_number


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
