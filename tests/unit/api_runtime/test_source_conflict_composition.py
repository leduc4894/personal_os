"""Composition of the source conflict runtime: the serve graph and its offline double.

These tests prove the serve composition binds the production adapters — the
durable PostgreSQL conflict store, the real policy-enforcement guard over
the shared enforcement service, and the verified evidence reader whose
PostgreSQL half resolves the exact expected object and whose R2 half opens
the fully verified reader behind a lazy per-process client — and never an
offline double, while the offline composition binds the real
:class:`SourceConflictService` over deterministic in-memory doubles with no
database, provider client or environment read. No database connection or
R2 request is opened: the adapters only capture the engine and the client
source at construction, which is exactly what lets the serve process
compose the graph before its first request.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from api_runtime.source_conflict_composition import (
    ConflictEvidenceDescriptor,
    OfflineConflictEvidenceReader,
    OfflineSourceConflictState,
    compose_offline_source_conflicts,
    compose_source_conflicts,
)
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.diagnostics.logging import DiagnosticLogger
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.source_conflicts.commands import (
    CaptureConflictCommand,
    ConflictResolutionResult,
    ResolveConflictCommand,
)
from personal_os.source_conflicts.contracts import (
    ConflictCandidate,
    ConflictEvidenceRole,
    ConflictIdempotencyKey,
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
    ConflictStatus,
    SourceConflict,
)
from personal_os.source_conflicts.errors import SourceConflictError
from personal_os.source_conflicts.service import SourceConflictService
from postgresql_source_store.conflict_store import PostgresqlSourceConflictStore
from r2_object_storage.adapter import R2S3ObjectStore
from r2_object_storage.settings import LoadedR2Credentials, ObjectStorageSettings

_R2_ENDPOINT = f"https://{'0' * 32}.r2.cloudflarestorage.com"
_WORKSPACE_ID = uuid4()
_DEVICE_ID = uuid4()
_CONFLICT_ID = uuid4()
_CAPTURED_AT = datetime(2026, 9, 2, 9, 15, 0, tzinfo=UTC)
_COMPLETED_AT = datetime(2026, 9, 2, 9, 40, 0, tzinfo=UTC)
_DIAGNOSTIC: DiagnosticContext = create_diagnostic_context().context


def _conflict(**overrides: Any) -> SourceConflict:
    fields: dict[str, Any] = dict(
        conflict_id=_CONFLICT_ID,
        workspace_id=_WORKSPACE_ID,
        source_id=uuid4(),
        conflict_kind=ConflictKind.STALE_CONTENT,
        status=ConflictStatus.OPEN,
        originating_event_id=uuid4(),
        originating_device_id=_DEVICE_ID,
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
    fields.update(overrides)
    return SourceConflict(**fields)


def _capture_command(conflict: SourceConflict) -> CaptureConflictCommand:
    return CaptureConflictCommand(
        workspace_id=conflict.workspace_id,
        source_id=conflict.source_id,
        conflict_kind=conflict.conflict_kind,
        originating_event_id=conflict.originating_event_id,
        originating_device_id=conflict.originating_device_id,
        idempotency_key=ConflictIdempotencyKey(str(uuid4())),
        base_version_id=conflict.base_version_id,
        observed_remote_version_id=conflict.observed_remote_version_id,
        candidate=conflict.candidate,
        normalized_locator=None,
    )


def _resolve_command(
    *, resolution_kind: ConflictResolutionKind = ConflictResolutionKind.KEEP_REMOTE
) -> ResolveConflictCommand:
    return ResolveConflictCommand(
        conflict_id=_CONFLICT_ID,
        reviewed_remote_version_id=None,
        resolution_kind=resolution_kind,
        resolution_event_id=uuid4(),
        idempotency_key=ConflictIdempotencyKey(str(uuid4())),
        verified_candidate_object_id=None,
    )


@pytest.fixture
def serve_engine() -> AsyncEngine:
    # The serve graph only captures the engine at construction — no
    # connection opens — so a psycopg engine over an unreachable address
    # composes exactly like the serving one.
    return create_async_engine("postgresql+psycopg://user:pass@127.0.0.1:5432/db")


def _serve_runtime(tmp_path: Path, engine: AsyncEngine) -> Any:
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
    return compose_source_conflicts(
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
    assert isinstance(runtime.service, SourceConflictService)
    assert isinstance(runtime.store, PostgresqlSourceConflictStore)
    assert isinstance(runtime.evidence._objects, R2S3ObjectStore)  # type: ignore[attr-defined]
    assert runtime.aclose is not None


def test_offline_composition_binds_the_real_service_over_doubles() -> None:
    state = OfflineSourceConflictState()
    runtime = compose_offline_source_conflicts(state=state)
    assert isinstance(runtime.service, SourceConflictService)
    assert not isinstance(runtime.store, PostgresqlSourceConflictStore)
    assert isinstance(runtime.evidence, OfflineConflictEvidenceReader)
    assert runtime.aclose is None


@pytest.mark.asyncio
async def test_offline_store_captures_and_replays_by_event_identity() -> None:
    state = OfflineSourceConflictState()
    runtime = compose_offline_source_conflicts(state=state)
    conflict = _conflict()
    command = _capture_command(conflict)
    captured = await runtime.store.capture(command, _DIAGNOSTIC)
    assert captured.source_id == conflict.source_id
    assert captured.conflict_kind is ConflictKind.STALE_CONTENT
    assert captured.captured_at is not None

    replayed = await runtime.store.capture(command, _DIAGNOSTIC)
    assert replayed == captured

    found = await runtime.store.find_captured_conflict(
        conflict.originating_event_id, conflict.workspace_id, _DIAGNOSTIC
    )
    assert found is not None and found.conflict_id == captured.conflict_id


@pytest.mark.asyncio
async def test_offline_store_lists_and_reads_within_the_workspace_scope() -> None:
    state = OfflineSourceConflictState(
        open_conflicts=(
            _conflict(),
            _conflict(conflict_id=uuid4(), workspace_id=uuid4()),
        )
    )
    runtime = compose_offline_source_conflicts(state=state)
    page = await runtime.store.list_open(
        _WORKSPACE_ID,
        limit=50,
        exclusive_start_conflict_id=None,
        diagnostic_context=_DIAGNOSTIC,
    )
    assert [entry.conflict_id for entry in page] == [_CONFLICT_ID]

    read = await runtime.store.read(_CONFLICT_ID, _WORKSPACE_ID, _DIAGNOSTIC)
    assert read.conflict_id == _CONFLICT_ID

    with pytest.raises(SourceConflictError) as foreign:
        await runtime.store.read(_CONFLICT_ID, uuid4(), _DIAGNOSTIC)
    assert foreign.value.error_code is ErrorCode.SOURCE_CONFLICT_NOT_FOUND


@pytest.mark.asyncio
async def test_offline_guard_denies_capture_and_resolution_when_seeded() -> None:
    state = OfflineSourceConflictState(is_policy_denied=True, open_conflicts=(_conflict(),))
    runtime = compose_offline_source_conflicts(state=state)
    with pytest.raises(ExclusionPolicyError) as capture_denied:
        await runtime.service.capture_conflict(_capture_command(_conflict()), _DIAGNOSTIC)
    assert capture_denied.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED

    with pytest.raises(ExclusionPolicyError) as resolve_denied:
        await runtime.service.resolve_conflict(
            _resolve_command(),
            workspace_id=_WORKSPACE_ID,
            diagnostic_context=_DIAGNOSTIC,
        )
    assert resolve_denied.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED


@pytest.mark.asyncio
async def test_offline_evidence_reader_streams_counts_opens_and_closes() -> None:
    payload = b"offline verified conflict evidence"
    state = OfflineSourceConflictState(evidence_bytes=payload)
    runtime = compose_offline_source_conflicts(state=state)
    assert runtime.evidence.open_count == 0
    chunks = [
        chunk
        async for chunk in runtime.evidence.open_evidence_stream(
            _CONFLICT_ID, ConflictEvidenceRole.BASE, _WORKSPACE_ID, _DIAGNOSTIC
        )
    ]
    assert b"".join(chunks) == payload
    assert runtime.evidence.open_count == 1
    assert state.evidence_reader_closed is True


@pytest.mark.asyncio
async def test_offline_evidence_catalog_fails_closed_for_unavailable_roles() -> None:
    state = OfflineSourceConflictState(
        evidence_unavailable_roles=frozenset({ConflictEvidenceRole.CANDIDATE})
    )
    runtime = compose_offline_source_conflicts(state=state)
    descriptor = await runtime.evidence_catalog.describe_evidence(
        _CONFLICT_ID, ConflictEvidenceRole.BASE, _WORKSPACE_ID, _DIAGNOSTIC
    )
    assert isinstance(descriptor, ConflictEvidenceDescriptor)
    assert descriptor.size_bytes == len(state.evidence_bytes)
    assert descriptor.media_type == "text/markdown"

    with pytest.raises(SourceConflictError) as unavailable:
        await runtime.evidence_catalog.describe_evidence(
            _CONFLICT_ID, ConflictEvidenceRole.CANDIDATE, _WORKSPACE_ID, _DIAGNOSTIC
        )
    assert unavailable.value.error_code is ErrorCode.SOURCE_CONFLICT_EVIDENCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_offline_resolve_double_answers_the_seeded_result() -> None:
    seeded = ConflictResolutionResult(
        kind=ConflictResolutionOutcome.RESOLVED,
        conflict_id=_CONFLICT_ID,
        resolution_event_id=uuid4(),
        resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
        resulting_version_id=None,
        successor=None,
        completed_at=_COMPLETED_AT,
    )
    state = OfflineSourceConflictState(open_conflicts=(_conflict(),), resolve_result=seeded)
    runtime = compose_offline_source_conflicts(state=state)
    result = await runtime.service.resolve_conflict(
        _resolve_command(), workspace_id=_WORKSPACE_ID, diagnostic_context=_DIAGNOSTIC
    )
    assert result == seeded
