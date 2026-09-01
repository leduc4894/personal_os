"""Composition of the source conflict runtime: the serve graph and its offline double.

:func:`compose_source_conflicts` builds the real serve graph the API process
runs: the durable PostgreSQL conflict store of the Child 8 aggregate, the
real :class:`~personal_os.exclusion_policy.enforcement.PolicyEnforcementService`
behind the shared
:class:`~api_runtime.small_file_sync_composition.PolicyEnforcementConflictCaptureGuard`
(re-evaluated before every capture, resolution and evidence read), the real
:class:`~personal_os.source_conflicts.service.SourceConflictService` binding
them with the in-memory low-cardinality metrics sink, and the verified
evidence reader whose PostgreSQL half resolves one role's exact expected
object inside the credential workspace — the object key and every other
provider-addressing column stay unread — while its R2 half opens the
existing fully verified spool-backed reader behind a lazy per-process
client. No connection opens at composition: the first store call does.

:func:`compose_offline_source_conflicts` builds the deterministic offline
graph used by the OpenAPI export and by route tests: an identity-keyed
in-memory store double mirroring the durable adapter semantics (capture
replay by originating event identity, workspace-scoped reads and pages, the
seeded resolution result), a policy-guard double honoring the denial knob,
and an evidence reader/catalog double serving one seeded verified buffer
whose open counter proves the reader only opens after authorization. It
reads no environment value, no secret file, no database and no provider
client, so the offline contract document stays byte-deterministic while
route tests seed behavior through the public knobs of
:class:`OfflineSourceConflictState` and observe safety through its captured
call evidence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier
from api_runtime.small_file_sync_composition import (
    LazyR2ClientSource,
    PolicyEnforcementConflictCaptureGuard,
)
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.logging import DiagnosticLogger
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.enforcement import default_utc_clock
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.metrics import ExclusionPolicyMetrics
from personal_os.object_storage import (
    CanonicalMediaType,
    CanonicalObjectStore,
    ContentDigest,
    ExpectedObject,
)
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.source_conflicts.commands import (
    CaptureConflictCommand,
    ConflictResolutionResult,
    ResolveConflictCommand,
)
from personal_os.source_conflicts.contracts import (
    ConflictCandidateKind,
    ConflictEvidenceRole,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
    ConflictStatus,
    SourceConflict,
)
from personal_os.source_conflicts.errors import SourceConflictError
from personal_os.source_conflicts.metrics import InMemorySourceConflictMetrics
from personal_os.source_conflicts.ports import (
    ConflictEvidenceReader,
    SourceConflictPolicyGuard,
    SourceConflictStore,
)
from personal_os.source_conflicts.service import SourceConflictService
from postgresql_source_store.conflict_store import (
    MAX_OPEN_CONFLICT_PAGE,
    PostgresqlSourceConflictStore,
)
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.policy_enforcement import compose_policy_enforcement
from postgresql_source_store.tables import (
    content_objects,
    source_versions,
)
from postgresql_source_store.tables import (
    source_conflicts as source_conflicts_table,
)
from r2_object_storage.adapter import R2S3ObjectStore
from r2_object_storage.client import R2ClientManager
from r2_object_storage.error_mapping import RetryPolicy
from r2_object_storage.metrics import InMemoryObjectStorageMetrics
from r2_object_storage.settings import LoadedR2Credentials, ObjectStorageSettings
from r2_object_storage.spool import SpoolManager

#: The object-storage failures that are the evidence path's own decided
#: integrity verdict; every other code is a retryable dependency outage.
_EVIDENCE_INTEGRITY_OBJECT_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED,
        ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT,
        ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID,
    }
)

#: The offline double's canonical media type of its seeded evidence bytes.
_OFFLINE_EVIDENCE_MEDIA_TYPE: Final[str] = "text/markdown"

#: The offline reader's chunk size, mirroring the verified reader contract.
_OFFLINE_EVIDENCE_CHUNK_BYTES: Final[int] = 65536


@dataclass(frozen=True, slots=True)
class ConflictEvidenceDescriptor:
    """One evidence role's safe canonical content metadata.

    Carries only the exact byte size and the canonical media type the
    verified read will deliver — never the digest, the object key or any
    provider detail; the digest stays internal to the verification request.
    """

    role: ConflictEvidenceRole
    size_bytes: int
    media_type: str


class ConflictEvidenceCatalog(Protocol):
    """The descriptor seam resolving one role's canonical content metadata.

    Satisfied structurally by
    :class:`PostgresqlConflictEvidenceReader` over the conflict's retained
    evidence and by the offline double over its seeded buffer. A role with
    no retained evidence fails closed with the typed conflict error.
    """

    async def describe_evidence(
        self,
        conflict_id: UUID,
        role: ConflictEvidenceRole,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> ConflictEvidenceDescriptor: ...


@dataclass(frozen=True, slots=True)
class SourceConflictRuntime:
    """One composed source conflict runtime the conflict routes consume.

    ``service`` orchestrates capture and resolution; ``store`` serves the
    safe reads (open listing and detail); ``policy_guard`` is the recheck
    the evidence stream must pass before any byte opens; ``evidence`` is
    the verified streaming reader; ``evidence_catalog`` resolves the safe
    content metadata the detail choices and the exact content headers
    need. ``aclose`` is the serve graph's disposal hook — closing the R2
    client and its spool reservations on shutdown; the offline graph owns
    no resource and leaves it unset.
    """

    service: SourceConflictService
    store: SourceConflictStore
    policy_guard: SourceConflictPolicyGuard
    evidence: ConflictEvidenceReader
    evidence_catalog: ConflictEvidenceCatalog
    aclose: Callable[[], Awaitable[None]] | None = None


def _map_evidence_object_failure(cause: ObjectStorageError) -> SourceConflictError:
    """Map one reader-path object failure onto the closed conflict registry.

    Ordinary absence under a canonical key means the retained evidence is no
    longer served — the closed ``evidence_unavailable`` verdict, never a
    substitute byte. The decided verification failures (digest/size
    corruption, stored metadata conflict, broken provider contract) are the
    evidence integrity verdict; every other code is the retryable
    dependency outage. The provider cause stays chained only.
    """

    if cause.error_code is ErrorCode.OBJECT_STORAGE_OBJECT_MISSING:
        return SourceConflictError(ErrorCode.SOURCE_CONFLICT_EVIDENCE_UNAVAILABLE)
    if cause.error_code in _EVIDENCE_INTEGRITY_OBJECT_CODES:
        return SourceConflictError(ErrorCode.SOURCE_CONFLICT_EVIDENCE_INTEGRITY_FAILED)
    return SourceConflictError(ErrorCode.SOURCE_CONFLICT_DEPENDENCY_UNAVAILABLE)


def _conflict_evidence_identity_statement(
    conflict_id: UUID, workspace_id: UUID
) -> sa.Select[tuple[Any, ...]]:
    """Build the workspace-scoped conflict evidence identity read.

    Selects only the role-resolving columns — never the locator snapshot,
    the idempotency keys or any audit material.
    """

    return sa.select(
        source_conflicts_table.c.conflict_id,
        source_conflicts_table.c.workspace_id,
        source_conflicts_table.c.base_version_id,
        source_conflicts_table.c.observed_remote_version_id,
        source_conflicts_table.c.candidate_kind,
        source_conflicts_table.c.verified_candidate_object_id,
    ).where(
        source_conflicts_table.c.conflict_id == conflict_id,
        source_conflicts_table.c.workspace_id == workspace_id,
    )


def _version_content_evidence_statement(
    workspace_id: UUID, source_version_id: UUID
) -> sa.Select[tuple[Any, ...]]:
    """Build the workspace-scoped exact-version content evidence read."""

    return (
        sa.select(
            content_objects.c.content_hash,
            content_objects.c.byte_size,
            content_objects.c.media_type,
        )
        .select_from(source_versions)
        .join(
            content_objects,
            content_objects.c.content_object_id == source_versions.c.content_object_id,
        )
        .where(
            source_versions.c.workspace_id == workspace_id,
            source_versions.c.source_version_id == source_version_id,
        )
    )


def _candidate_content_evidence_statement(content_object_id: UUID) -> sa.Select[tuple[Any, ...]]:
    """Build the content-object evidence read for the retained candidate.

    The conflict row already proved the workspace scope of its candidate
    reference; the object row itself is content-addressed and carries only
    the canonical verification material.
    """

    return sa.select(
        content_objects.c.content_hash,
        content_objects.c.byte_size,
        content_objects.c.media_type,
    ).where(content_objects.c.content_object_id == content_object_id)


class PostgresqlConflictEvidenceReader:
    """Verified evidence reading over canonical state and the object store.

    One ``READ COMMITTED`` transaction resolves the role's exact expected
    object inside the credential workspace — the workspace-scoped conflict
    row first (a foreign workspace's conflict is indistinguishable from
    missing), then the retained version or candidate object's canonical
    digest, size and media type; a role with no retained evidence fails
    closed with the typed ``evidence_unavailable`` verdict before any byte
    is fetched. The verified reader context of the existing spool-backed
    object store performs the exact HEAD plus conditional full GET
    verification on entry, and exact bytes flow only from that verified
    spool. SQLSTATE, SQL text, parameters, driver messages, object keys and
    digests never leave the adapter.
    """

    def __init__(self, *, engine: AsyncEngine, objects: CanonicalObjectStore) -> None:
        self._engine = engine
        self._objects = objects

    async def describe_evidence(
        self,
        conflict_id: UUID,
        role: ConflictEvidenceRole,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> ConflictEvidenceDescriptor:
        """Resolve one role's safe canonical content metadata."""

        del diagnostic_context
        expected = await self._resolve_expected_object(conflict_id, role, workspace_id)
        return ConflictEvidenceDescriptor(
            role=role,
            size_bytes=expected.size_bytes,
            media_type=expected.media_type.value,
        )

    async def open_evidence_stream(
        self,
        conflict_id: UUID,
        role: ConflictEvidenceRole,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> AsyncIterator[bytes]:
        """Stream one role's exact verified bytes.

        The expected object resolves — and every typed failure closes —
        before the verified reader context is entered, so the caller can
        prime the first chunk inside the endpoint and keep pre-stream
        failures as canonical JSON envelopes. The verified context stays
        open for the whole iteration and closes on completion, error or
        generator close.
        """

        expected = await self._resolve_expected_object(conflict_id, role, workspace_id)
        try:
            async with self._objects.open_verified_reader(expected) as reader:
                async for chunk in reader:
                    yield chunk
        except ObjectStorageError as cause:
            raise _map_evidence_object_failure(cause) from cause

    async def _resolve_expected_object(
        self,
        conflict_id: UUID,
        role: ConflictEvidenceRole,
        workspace_id: UUID,
    ) -> ExpectedObject:
        """Resolve one role's exact verification request, or reject typed."""

        try:
            async with (
                self._engine.connect() as connection,
                connection.begin(),
            ):
                await apply_transaction_bounds(connection)
                identity_row = (
                    await connection.execute(
                        _conflict_evidence_identity_statement(conflict_id, workspace_id)
                    )
                ).one_or_none()
                if identity_row is None:
                    raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_NOT_FOUND)
                content_row = await self._resolve_content_row(
                    connection, identity_row, role, workspace_id
                )
        except sa.exc.SQLAlchemyError:
            raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_DEPENDENCY_UNAVAILABLE) from None
        if content_row is None:
            raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_EVIDENCE_UNAVAILABLE)
        try:
            return ExpectedObject(
                content_digest=ContentDigest.parse(str(content_row[0])),
                size_bytes=int(content_row[1]),
                media_type=CanonicalMediaType.parse(str(content_row[2])),
            )
        except ValueError:
            raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_EVIDENCE_INTEGRITY_FAILED) from None

    async def _resolve_content_row(
        self,
        connection: Any,
        identity_row: Any,
        role: ConflictEvidenceRole,
        workspace_id: UUID,
    ) -> Any:
        """Resolve the role's content evidence row over the locked identity."""

        if role is ConflictEvidenceRole.BASE:
            base_version_id = identity_row.base_version_id
            if base_version_id is None:
                return None
            return (
                await connection.execute(
                    _version_content_evidence_statement(workspace_id, base_version_id)
                )
            ).one_or_none()
        if role is ConflictEvidenceRole.REMOTE:
            observed_remote_version_id = identity_row.observed_remote_version_id
            if observed_remote_version_id is None:
                return None
            return (
                await connection.execute(
                    _version_content_evidence_statement(workspace_id, observed_remote_version_id)
                )
            ).one_or_none()
        if str(identity_row.candidate_kind) != ConflictCandidateKind.CONTENT.value:
            # A delete candidate retains no bytes; the reader fails closed
            # and never substitutes another role's bytes.
            return None
        verified_candidate_object_id = identity_row.verified_candidate_object_id
        if verified_candidate_object_id is None:
            return None
        return (
            await connection.execute(
                _candidate_content_evidence_statement(verified_candidate_object_id)
            )
        ).one_or_none()


def compose_source_conflicts(
    *,
    engine: AsyncEngine,
    object_storage_settings: ObjectStorageSettings,
    object_storage_credentials: LoadedR2Credentials,
    logger: DiagnosticLogger,
    policy_metrics: ExclusionPolicyMetrics | None = None,
) -> SourceConflictRuntime:
    """Build the real serve runtime of one API process.

    Follows the small-file-sync serve precedent's shape: the shared engine,
    the durable conflict store that connects lazily per transaction, the
    enforcement service over each canonical snapshot's persisted trust
    anchor and the provider adapters that open no connection at
    construction — the R2 client opens at the first verified evidence read
    inside the serving loop. The graph is therefore composable before the
    socket exists while every adapter is the production one.
    """

    enforcement = compose_policy_enforcement(
        engine, verifier=TrustAnchorEd25519Verifier(), metrics=policy_metrics
    )
    store = PostgresqlSourceConflictStore(engine, clock=default_utc_clock)
    policy_guard = PolicyEnforcementConflictCaptureGuard(enforcement=enforcement)
    object_store = R2S3ObjectStore(
        LazyR2ClientSource(R2ClientManager(object_storage_settings, object_storage_credentials)),
        spools=SpoolManager(object_storage_settings.object_storage_spool_root),
        retry=RetryPolicy(),
        metrics=InMemoryObjectStorageMetrics(),
        logger=logger,
    )
    evidence_reader = PostgresqlConflictEvidenceReader(engine=engine, objects=object_store)
    service = SourceConflictService(
        store=store,
        policy_guard=policy_guard,
        metrics=InMemorySourceConflictMetrics(),
        clock=default_utc_clock,
    )
    return SourceConflictRuntime(
        service=service,
        store=store,
        policy_guard=policy_guard,
        evidence=evidence_reader,
        evidence_catalog=evidence_reader,
        aclose=object_store.close,
    )


class OfflineConflictClock:
    """Aware UTC clock defaulting to the real clock."""

    def __call__(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class OfflineSourceConflictState:
    """Public knobs and captured evidence of the offline conflict graph.

    Tests seed behavior through ``open_conflicts`` (the store's workspace-
    scoped rows), the typed error knobs (``None`` keeps the happy default),
    the policy denial knob, the seeded resolution result and the evidence
    knobs (``evidence_bytes``, ``evidence_media_type``,
    ``evidence_unavailable_roles``, ``evidence_error``), and read safety
    back through the captured call evidence and the reader's open counter.
    The doubles never retain a locator, key or digest.
    """

    open_conflicts: tuple[SourceConflict, ...] = ()
    read_error: SourceConflictError | None = None
    list_error: SourceConflictError | None = None
    resolve_error: SourceConflictError | None = None
    resolve_result: ConflictResolutionResult | None = None
    is_policy_denied: bool = False
    evidence_bytes: bytes = b"offline source conflict evidence bytes"
    evidence_media_type: str = _OFFLINE_EVIDENCE_MEDIA_TYPE
    evidence_error: SourceConflictError | None = None
    evidence_unavailable_roles: frozenset[ConflictEvidenceRole] = frozenset()
    evidence_open_count: int = 0
    evidence_reader_closed: bool = False
    list_calls: list[tuple[UUID, int, UUID | None]] = field(default_factory=list)
    read_calls: list[tuple[UUID, UUID]] = field(default_factory=list)
    resolve_calls: list[tuple[ResolveConflictCommand, UUID]] = field(default_factory=list)
    authorize_calls: list[UUID] = field(default_factory=list)
    describe_calls: list[tuple[UUID, ConflictEvidenceRole, UUID]] = field(default_factory=list)
    #: The offline mirror of the conflict store's event-identity replay map.
    captured_by_event: dict[tuple[UUID, UUID], SourceConflict] = field(default_factory=dict)


class OfflineSourceConflictStore:
    """In-memory conflict store double mirroring the durable adapter semantics.

    Reads and pages are workspace-scoped (a foreign workspace's conflict is
    indistinguishable from missing), capture freezes one conflict per
    (workspace, event) identity and replays it unchanged, and resolve
    answers the seeded result or the derived winner exactly once per event
    identity.
    """

    def __init__(self, state: OfflineSourceConflictState) -> None:
        self._state = state

    def _stored(self, conflict_id: UUID, workspace_id: UUID) -> SourceConflict | None:
        for conflict in self._state.open_conflicts:
            if conflict.conflict_id == conflict_id:
                return conflict if conflict.workspace_id == workspace_id else None
        return None

    async def capture(
        self,
        command: CaptureConflictCommand,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict:
        del diagnostic_context
        identity = (command.workspace_id, command.originating_event_id)
        stored = self._state.captured_by_event.get(identity)
        if stored is not None:
            return stored
        conflict = SourceConflict(
            conflict_id=uuid4(),
            workspace_id=command.workspace_id,
            source_id=command.source_id,
            conflict_kind=command.conflict_kind,
            status=ConflictStatus.OPEN,
            originating_event_id=command.originating_event_id,
            originating_device_id=command.originating_device_id,
            base_version_id=command.base_version_id,
            observed_remote_version_id=command.observed_remote_version_id,
            candidate=command.candidate,
            captured_at=datetime.now(UTC),
            resolution_kind=None,
            resolution_event_id=None,
            resulting_version_id=None,
            successor_conflict_id=None,
            closed_at=None,
        )
        self._state.captured_by_event[identity] = conflict
        return conflict

    async def find_captured_conflict(
        self,
        originating_event_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict | None:
        del diagnostic_context
        return self._state.captured_by_event.get((workspace_id, originating_event_id))

    async def list_open(
        self,
        workspace_id: UUID,
        *,
        limit: int,
        exclusive_start_conflict_id: UUID | None,
        diagnostic_context: DiagnosticContext,
    ) -> tuple[SourceConflict, ...]:
        del diagnostic_context
        if not 1 <= limit <= MAX_OPEN_CONFLICT_PAGE:
            raise ValueError("limit must be between 1 and the closed page bound")
        self._state.list_calls.append((workspace_id, limit, exclusive_start_conflict_id))
        if self._state.list_error is not None:
            raise self._state.list_error
        entries = sorted(
            (
                conflict
                for conflict in self._state.open_conflicts
                if conflict.workspace_id == workspace_id
                and conflict.status is ConflictStatus.OPEN
                and (
                    exclusive_start_conflict_id is None
                    or conflict.conflict_id > exclusive_start_conflict_id
                )
            ),
            key=lambda conflict: conflict.conflict_id,
        )
        return tuple(entries[:limit])

    async def read(
        self,
        conflict_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict:
        return await self._read(conflict_id, workspace_id, diagnostic_context)

    async def read_for_resolution(
        self,
        conflict_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict:
        return await self._read(conflict_id, workspace_id, diagnostic_context)

    async def _read(
        self,
        conflict_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict:
        del diagnostic_context
        self._state.read_calls.append((conflict_id, workspace_id))
        if self._state.read_error is not None:
            raise self._state.read_error
        conflict = self._stored(conflict_id, workspace_id)
        if conflict is None:
            raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_NOT_FOUND)
        return conflict

    async def resolve(
        self,
        command: ResolveConflictCommand,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> ConflictResolutionResult:
        del diagnostic_context, workspace_id
        self._state.resolve_calls.append((command, command.conflict_id))
        if self._state.resolve_error is not None:
            raise self._state.resolve_error
        if self._state.resolve_result is not None:
            return self._state.resolve_result
        return ConflictResolutionResult(
            kind=ConflictResolutionOutcome.RESOLVED,
            conflict_id=command.conflict_id,
            resolution_event_id=command.resolution_event_id,
            resolution_kind=command.resolution_kind,
            resulting_version_id=(
                uuid4()
                if command.resolution_kind
                in (
                    ConflictResolutionKind.KEEP_LOCAL,
                    ConflictResolutionKind.SAVE_MERGED,
                )
                else None
            ),
            successor=None,
            completed_at=datetime.now(UTC),
        )


class OfflineConflictPolicyGuard:
    """Policy-guard double honoring the state's denial knob."""

    def __init__(self, state: OfflineSourceConflictState) -> None:
        self._state = state

    async def authorize_capture(
        self,
        command: CaptureConflictCommand,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del command, diagnostic_context
        if self._state.is_policy_denied:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)

    async def authorize_resolution(
        self,
        conflict: SourceConflict,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del diagnostic_context
        if self._state.is_policy_denied:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)
        self._state.authorize_calls.append(conflict.conflict_id)


class OfflineConflictEvidenceReader:
    """Verified evidence reader double over the state's seeded buffer.

    The open counter increments only when the stream actually starts — the
    generator body runs on the first chunk request — so a denial raised
    before the stream opens leaves the counter untouched, which is exactly
    the ordering the route's policy recheck must prove.
    """

    def __init__(self, state: OfflineSourceConflictState) -> None:
        self._state = state

    @property
    def open_count(self) -> int:
        return self._state.evidence_open_count

    async def open_evidence_stream(
        self,
        conflict_id: UUID,
        role: ConflictEvidenceRole,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> AsyncIterator[bytes]:
        del conflict_id, workspace_id, diagnostic_context
        self._state.evidence_open_count += 1
        try:
            if self._state.evidence_error is not None:
                raise self._state.evidence_error
            if role in self._state.evidence_unavailable_roles:
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_EVIDENCE_UNAVAILABLE)
            remaining = self._state.evidence_bytes
            while remaining:
                yield remaining[:_OFFLINE_EVIDENCE_CHUNK_BYTES]
                remaining = remaining[_OFFLINE_EVIDENCE_CHUNK_BYTES:]
        finally:
            self._state.evidence_reader_closed = True


class OfflineConflictEvidenceCatalog:
    """Evidence catalog double serving the seeded content metadata."""

    def __init__(self, state: OfflineSourceConflictState) -> None:
        self._state = state

    async def describe_evidence(
        self,
        conflict_id: UUID,
        role: ConflictEvidenceRole,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> ConflictEvidenceDescriptor:
        del diagnostic_context
        self._state.describe_calls.append((conflict_id, role, workspace_id))
        if role in self._state.evidence_unavailable_roles:
            raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_EVIDENCE_UNAVAILABLE)
        return ConflictEvidenceDescriptor(
            role=role,
            size_bytes=len(self._state.evidence_bytes),
            media_type=self._state.evidence_media_type,
        )


def compose_offline_source_conflicts(
    *,
    state: OfflineSourceConflictState | None = None,
) -> SourceConflictRuntime:
    """Build the deterministic offline source conflict runtime."""

    offline_state = state if state is not None else OfflineSourceConflictState()
    store = OfflineSourceConflictStore(offline_state)
    policy_guard = OfflineConflictPolicyGuard(offline_state)
    return SourceConflictRuntime(
        service=SourceConflictService(
            store=store,
            policy_guard=policy_guard,
            metrics=InMemorySourceConflictMetrics(),
            clock=OfflineConflictClock(),
        ),
        store=store,
        policy_guard=policy_guard,
        evidence=OfflineConflictEvidenceReader(offline_state),
        evidence_catalog=OfflineConflictEvidenceCatalog(offline_state),
    )


__all__ = [
    "ConflictEvidenceCatalog",
    "ConflictEvidenceDescriptor",
    "OfflineConflictClock",
    "OfflineConflictEvidenceCatalog",
    "OfflineConflictEvidenceReader",
    "OfflineConflictPolicyGuard",
    "OfflineSourceConflictState",
    "OfflineSourceConflictStore",
    "PostgresqlConflictEvidenceReader",
    "SourceConflictRuntime",
    "compose_offline_source_conflicts",
    "compose_source_conflicts",
]
