"""Composition of the device sync runtime: the serve graph and its offline double.

These tests prove the serve composition binds the production adapters — the
durable PostgreSQL device event store, manifest store and content catalog of
the schema child, and the real R2 verified object reader with its lazy
per-process client — and never an offline double, while the offline
composition binds the real domain services over deterministic in-memory
doubles with no database, provider client or environment read. No database
connection or R2 request is opened: the adapters only capture the engine and
the client source at construction, which is exactly what lets the serve
process compose the graph before its first request.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from api_runtime.device_sync_composition import (
    DeviceSyncRuntime,
    OfflineDeviceSyncState,
    compose_device_sync,
    compose_offline_device_sync,
)
from api_runtime.device_sync_content import VerifiedDeviceContentService
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from personal_os.device_sync.contracts import (
    MAX_MANIFEST_PAGE_ENTRIES,
    DeviceCursorReceipt,
    DeviceEventPage,
    DeviceSyncContext,
    ManifestActionsQuery,
    ManifestRunState,
    StartManifestCommand,
)
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.device_sync.service import DeviceSyncService
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.diagnostics.logging import DiagnosticLogger
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.runtime_configuration.models import RuntimeEnvironment
from postgresql_source_store.device_content_catalog import PostgresqlDeviceContentCatalog
from postgresql_source_store.device_event_store import PostgresqlDeviceEventStore
from postgresql_source_store.device_manifest_store import PostgresqlDeviceManifestStore
from r2_object_storage.adapter import R2S3ObjectStore
from r2_object_storage.settings import LoadedR2Credentials, ObjectStorageSettings

_R2_ENDPOINT = f"https://{'0' * 32}.r2.cloudflarestorage.com"
_WORKSPACE_ID = uuid4()
_DEVICE_ID = uuid4()
_USER_ID = uuid4()
_SOURCE_ID = uuid4()
_SOURCE_VERSION_ID = uuid4()
_DIAGNOSTIC: DiagnosticContext = create_diagnostic_context().context


def _context() -> DeviceSyncContext:
    return DeviceSyncContext(workspace_id=_WORKSPACE_ID, device_id=_DEVICE_ID, user_id=_USER_ID)


@pytest.fixture
def serve_engine() -> AsyncEngine:
    # The serve graph only captures the engine at construction — no
    # connection opens — so a psycopg engine over an unreachable address
    # composes exactly like the serving one.
    return create_async_engine("postgresql+psycopg://user:pass@127.0.0.1:5432/db")


def _serve_runtime(tmp_path: Path, engine: AsyncEngine) -> DeviceSyncRuntime:
    spool_root = tmp_path / "spool"
    spool_root.mkdir(exist_ok=True)
    settings = ObjectStorageSettings(
        environment=RuntimeEnvironment.LOCAL,
        secret_root=tmp_path,
        r2_endpoint=_R2_ENDPOINT,
        r2_bucket_name="personal-knowledge-objects",
        r2_access_key_id_file="r2_access_key_id",
        r2_secret_access_key_file="r2_secret_access_key",
        object_storage_spool_root=spool_root,
    )
    return compose_device_sync(
        engine=engine,
        object_storage_settings=settings,
        object_storage_credentials=LoadedR2Credentials(
            access_key_id=SecretStr("access-key-id"),
            secret_access_key=SecretStr("secret-access-key"),
        ),
        logger=DiagnosticLogger({"service": "api", "environment": "local"}),
    )


def test_serve_composition_binds_the_production_adapters(
    tmp_path: Path, serve_engine: AsyncEngine
) -> None:
    runtime = _serve_runtime(tmp_path, serve_engine)
    assert isinstance(runtime.service, DeviceSyncService)
    assert isinstance(runtime.service._events, PostgresqlDeviceEventStore)
    assert isinstance(runtime.service._manifests, PostgresqlDeviceManifestStore)
    assert isinstance(runtime.content, VerifiedDeviceContentService)
    assert isinstance(runtime.content._catalog, PostgresqlDeviceContentCatalog)
    assert isinstance(runtime.content._objects, R2S3ObjectStore)
    assert runtime.aclose is not None


def test_offline_composition_binds_the_real_services_over_doubles() -> None:
    state = OfflineDeviceSyncState()
    runtime = compose_offline_device_sync(state=state)
    assert isinstance(runtime.service, DeviceSyncService)
    assert isinstance(runtime.content, VerifiedDeviceContentService)
    assert not isinstance(runtime.service._events, PostgresqlDeviceEventStore)
    assert not isinstance(runtime.content._catalog, PostgresqlDeviceContentCatalog)
    assert runtime.aclose is None


@pytest.mark.asyncio
async def test_offline_double_pull_and_acknowledge_follow_the_seeded_state() -> None:
    state = OfflineDeviceSyncState()
    state.pull_page = DeviceEventPage(
        acknowledged_sequence=0,
        page_checkpoint_sequence=0,
        delivered_through_sequence=0,
        events=(),
        has_more=False,
    )
    state.acknowledge_receipt = DeviceCursorReceipt(2, 5)
    runtime = compose_offline_device_sync(state=state)

    page = await runtime.service.pull_events(
        context=_context(), diagnostic_context=_DIAGNOSTIC
    )
    assert page.acknowledged_sequence == 0
    assert state.pull_contexts == [_context()]

    receipt = await runtime.service.acknowledge_cursor(
        context=_context(),
        expected_previous_sequence=2,
        applied_through_sequence=5,
        diagnostic_context=_DIAGNOSTIC,
    )
    assert receipt.acknowledged_sequence == 2


@pytest.mark.asyncio
async def test_offline_content_double_honors_the_policy_and_error_knobs() -> None:
    state = OfflineDeviceSyncState()
    state.content_error = DeviceSyncError(DeviceSyncErrorCode.EVENT_UNAVAILABLE)
    runtime = compose_offline_device_sync(state=state)
    with pytest.raises(DeviceSyncError) as unavailable:
        async with runtime.content.open_content(
            _context(),
            source_id=_SOURCE_ID,
            source_version_id=_SOURCE_VERSION_ID,
            diagnostic_context=_DIAGNOSTIC,
        ):
            raise AssertionError("the unavailable descriptor must never open a reader")
    assert unavailable.value.code is DeviceSyncErrorCode.EVENT_UNAVAILABLE

    state.content_error = None
    state.is_policy_denied = True
    with pytest.raises(ExclusionPolicyError) as denied:
        async with runtime.content.open_content(
            _context(),
            source_id=_SOURCE_ID,
            source_version_id=_SOURCE_VERSION_ID,
            diagnostic_context=_DIAGNOSTIC,
        ):
            raise AssertionError("the denied descriptor must never open a reader")
    assert denied.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED


@pytest.mark.asyncio
async def test_offline_content_double_streams_verified_bytes_and_closes() -> None:
    payload = b"offline verified device content bytes"
    state = OfflineDeviceSyncState(content_bytes=payload)
    runtime = compose_offline_device_sync(state=state)
    async with runtime.content.open_content(
        _context(),
        source_id=_SOURCE_ID,
        source_version_id=_SOURCE_VERSION_ID,
        diagnostic_context=_DIAGNOSTIC,
    ) as content:
        assert content.descriptor.size_bytes == len(payload)
        assert content.descriptor.content_digest.hexadecimal == sha256(payload).hexdigest()
        streamed = b"".join([chunk async for chunk in content.reader])
    assert streamed == payload
    assert state.reader_closed is True


@pytest.mark.asyncio
async def test_offline_manifest_double_defaults_are_deterministic() -> None:
    state = OfflineDeviceSyncState()
    runtime = compose_offline_device_sync(state=state)
    started = await runtime.service.start_manifest(
        StartManifestCommand(
            context=_context(),
            client_observation_generation=3,
            diagnostic_context=_DIAGNOSTIC,
        )
    )
    assert started.state is ManifestRunState.COLLECTING
    assert started.client_observation_generation == 3
    assert started.expires_at > datetime.now(UTC)

    actions = await runtime.service.read_manifest_actions(
        ManifestActionsQuery(
            context=_context(),
            manifest_run_id=started.manifest_run_id,
            after_action_index=0,
            limit=MAX_MANIFEST_PAGE_ENTRIES,
            diagnostic_context=_DIAGNOSTIC,
        )
    )
    assert actions.actions == ()
    assert actions.has_more is False
