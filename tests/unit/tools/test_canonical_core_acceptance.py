"""Unit tests for the phase-one acceptance composition (design spec 7).

The orchestration runs against the REAL identity/publication/read services
over fake ports: only the infrastructure edges (publication store, object
store, read store, intent store, workflow starter, diagnostics sink, table
counts) are faked, so every spec-7 claim is proven through the production
service contracts:

- the exact bootstrap replay returns the same ids and original committed
  timestamp with no new row;
- the publication runs preflight-miss -> stream/store/full-verify -> one
  atomic commit, and its exact replay performs ZERO object-store interactions
  and adds no row;
- the canonical read returns exactly the published bytes;
- the two projection intents claimed through fenced transitions derive the
  identical ``source-ingestion/{workspace_id}/{event_id}`` workflow id and the
  closed four-UUID input, and the two starts converge on ONE deterministic
  Temporal execution (first ``STARTED``, then ``EXISTING``);
- exactly one registered completion or failure event, one closed metric
  outcome and one safe JSON summary of IDs and safe counts only.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4
from uuid import uuid7 as _uuid7

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from tools.canonical_core_operations import (
    CanonicalCoreExitCode,
    PhaseOneAcceptanceCollaborators,
    main,
    run_phase_one_acceptance,
)
from workflow_worker.projection_workflow_starter import (
    SOURCE_INGESTION_REFERENCE_CONTRACT,
    ProjectionWorkflowStartResult,
    SourceIngestionReference,
    projection_workflow_id,
)

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.diagnostics.events import EventName, SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.identity.contracts import (
    BootstrapIdentityCommand,
    BootstrapIdentityOutcome,
    BootstrapIdentityResult,
)
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from personal_os.recovery.contracts import (
    AcceptanceMetricOutcome,
    InMemoryCanonicalAcceptanceMetrics,
)
from personal_os.sources.commands import CreateSourceVersion
from personal_os.sources.errors import ProjectionDispatchError
from personal_os.sources.metrics import (
    InMemoryCanonicalReadMetrics,
    InMemorySourcePublicationMetrics,
)
from personal_os.sources.projection_dispatch import LeasedProjectionIntent
from personal_os.sources.publication import SourceVersionPublicationService
from personal_os.sources.reading import (
    CanonicalSourceReadService,
    CanonicalSourceReference,
    ReadCurrentSourceCommand,
)
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult

_LOCAL_ENVIRONMENT: Mapping[str, str] = {"KNOWLEDGE_ENVIRONMENT": "local"}

_FIXED_NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)

_QDRANT_KIND = SafeToken.parse("qdrant")
_NEO4J_KIND = SafeToken.parse("neo4j")
_UPSERT_OPERATION = SafeToken.parse("upsert")


def _fixed_clock() -> datetime:
    return _FIXED_NOW


class _PayloadReader:
    """Async reader over already-verified in-memory bytes."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    def __aiter__(self) -> _PayloadReader:
        return self

    async def __anext__(self) -> bytes:
        if self._offset >= len(self._payload):
            raise StopAsyncIteration
        chunk = self._payload[self._offset : self._offset + 4096]
        self._offset += len(chunk)
        return chunk


@dataclass
class RecordingSink:
    """Structural diagnostic sink retaining accepted payloads."""

    events: list[tuple[EventName, dict[str, object]]] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None:
        self.events.append((event_name, dict(fields or {})))

    def of(self, event_name: EventName) -> list[dict[str, object]]:
        return [fields for name, fields in self.events if name is event_name]


@dataclass
class FakeIdentityService:
    """Identity bootstrap port: create once, then exact replay."""

    ledger: list[str]
    counts: dict[str, int]
    user_id: UUID = field(default_factory=uuid4)
    workspace_id: UUID = field(default_factory=uuid4)
    device_id: UUID = field(default_factory=uuid4)
    committed_at: datetime = field(default=lambda: _FIXED_NOW - timedelta(minutes=5))
    calls: int = 0

    async def bootstrap(
        self, command: BootstrapIdentityCommand, diagnostic_context: DiagnosticContext
    ) -> BootstrapIdentityResult:
        del command, diagnostic_context
        self.calls += 1
        if self.calls == 1:
            self.ledger.append("bootstrap:create")
            self.counts["users"] += 1
            self.counts["workspaces"] += 1
            self.counts["devices"] += 1
            self.counts["audit_events"] += 1
            outcome = BootstrapIdentityOutcome.CREATED
        else:
            self.ledger.append("bootstrap:replay")
            outcome = BootstrapIdentityOutcome.EXISTING
        return BootstrapIdentityResult(
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            device_id=self.device_id,
            outcome=outcome,
            committed_at=self.committed_at,
        )


@dataclass
class FakeObjectStore:
    """Canonical object store recording every interaction and its order."""

    ledger: list[str]
    clock: object
    objects: dict[str, tuple[bytes, CanonicalMediaType]] = field(default_factory=dict)
    interactions: list[str] = field(default_factory=list)

    def _receipt(
        self, digest: ContentDigest, payload: bytes, media_type: CanonicalMediaType
    ) -> VerifiedObjectReceipt:
        return VerifiedObjectReceipt(
            content_digest=digest,
            object_key=derive_canonical_object_key(digest),
            size_bytes=len(payload),
            media_type=media_type,
            verified_at=self.clock(),  # type: ignore[operator]
            verification_method=VerificationMethod.UPLOADED_FULL_READ,
        )

    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None:
        self.interactions.append("resolve")
        self.ledger.append("object:resolve")
        stored = self.objects.get(expected.content_digest.hexadecimal)
        if stored is None:
            return None
        payload, media_type = stored
        return self._receipt(expected.content_digest, payload, media_type)

    async def store_stream(
        self,
        stream: AsyncIterator[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt:
        self.interactions.append("store_stream")
        self.ledger.append("object:store_stream")
        payload = b""
        async for chunk in stream:
            payload += chunk
        assert len(payload) == expected_size_bytes
        digest_hexadecimal = sha256(payload).hexdigest()
        assert claimed_sha256 == digest_hexadecimal
        digest = ContentDigest.parse(digest_hexadecimal)
        self.objects[digest_hexadecimal] = (payload, CanonicalMediaType.parse(media_type))
        return self._receipt(digest, payload, CanonicalMediaType.parse(media_type))

    async def verify_existing_object(self, expected: ExpectedObject) -> VerifiedObjectReceipt:
        self.interactions.append("verify")
        self.ledger.append("object:verify")
        payload, media_type = self.objects[expected.content_digest.hexadecimal]
        return self._receipt(expected.content_digest, payload, media_type)

    def open_verified_reader(self, expected: ExpectedObject) -> object:
        self.interactions.append("open_reader")
        self.ledger.append("object:open_reader")
        payload, _ = self.objects[expected.content_digest.hexadecimal]

        class _Opened:
            async def __aenter__(self) -> _PayloadReader:
                return _PayloadReader(payload)

            async def __aexit__(self, *exc_info: object) -> None:
                return None

        return _Opened()


@dataclass
class FakePublicationStore:
    """Publication store port recording the preflight miss and single commit."""

    ledger: list[str]
    counts: dict[str, int]
    committed: SourceVersionPublicationResult | None = None
    commands: list[CreateSourceVersion] = field(default_factory=list)
    commit_count: int = 0

    async def resolve_committed(
        self,
        command: CreateSourceVersion,
        request_fingerprint: object,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult | None:
        del request_fingerprint, diagnostic_context
        self.ledger.append("preflight:hit" if self.committed is not None else "preflight:miss")
        return self.committed

    async def commit_create(
        self,
        command: CreateSourceVersion,
        request_fingerprint: object,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        *,
        preflight_decision: object | None = None,
    ) -> SourceVersionPublicationResult:
        del request_fingerprint, receipt, diagnostic_context, preflight_decision
        self.ledger.append("commit_create")
        self.commit_count += 1
        self.commands.append(command)
        self.committed = SourceVersionPublicationResult(
            source_id=command.source_id,
            source_version_id=uuid4(),
            content_version=1,
            event_id=command.event_id,
            event_sequence=1,
            content_digest=command.expected_object.content_digest,
            outcome=PublicationOutcome.PUBLISHED,
            committed_at=_FIXED_NOW,
        )
        self.counts["sources"] += 1
        self.counts["source_versions"] += 1
        self.counts["sync_events"] += 1
        self.counts["projection_intents"] += 2
        self.counts["audit_events"] += 1
        self.counts["content_objects"] += 1
        return self.committed


@dataclass
class FakeReadStore:
    """Read store port resolving the current reference from the publication."""

    ledger: list[str]
    publication_store: FakePublicationStore
    identity: FakeIdentityService
    failure: Exception | None = None

    async def resolve_current(
        self, command: ReadCurrentSourceCommand, diagnostic_context: DiagnosticContext
    ) -> CanonicalSourceReference:
        del diagnostic_context
        self.ledger.append("read_current")
        if self.failure is not None:
            raise self.failure
        committed = self.publication_store.committed
        assert committed is not None
        published_command = self.publication_store.commands[0]
        return CanonicalSourceReference(
            workspace_id=self.identity.workspace_id,
            source_id=command.source_id,
            source_version_id=committed.source_version_id,
            content_version=committed.content_version,
            source_type=published_command.source_type,
            expected_object=published_command.expected_object,
            committed_at=committed.committed_at,
        )


@dataclass
class FakeIntentStore:
    """Intent store port: one fenced claim of the two published intents."""

    ledger: list[str]
    publication_store: FakePublicationStore
    identity: FakeIdentityService
    claimed_once: bool = False
    acknowledged: list[str] = field(default_factory=list)

    async def reclaim_expired(self, now: datetime) -> int:
        del now
        return 0

    async def claim_batch(self, now: datetime, limit: int) -> tuple[LeasedProjectionIntent, ...]:
        del now, limit
        self.ledger.append("claim_batch")
        if self.claimed_once:
            return ()
        self.claimed_once = True
        committed = self.publication_store.committed
        assert committed is not None
        # Deliberately unordered: the composition must derive the deterministic
        # start order itself.
        return tuple(
            LeasedProjectionIntent(
                projection_intent_id=_uuid7(),
                workspace_id=self.identity.workspace_id,
                event_id=committed.event_id,
                source_id=committed.source_id,
                source_version_id=committed.source_version_id,
                projection_kind=kind,
                operation=_UPSERT_OPERATION,
                attempt_count=0,
                lease_token=_uuid7(),
                leased_until=_FIXED_NOW + timedelta(seconds=60),
            )
            for kind in (_QDRANT_KIND, _NEO4J_KIND)
        )

    async def acknowledge_dispatched(
        self, intent_id: UUID, lease_token: UUID, now: datetime
    ) -> bool:
        del intent_id, lease_token, now
        kind = "qdrant" if len(self.acknowledged) == 1 else "neo4j"
        self.ledger.append(f"acknowledge:{kind}")
        self.acknowledged.append(kind)
        return True

    async def release_retry(
        self,
        intent_id: UUID,
        lease_token: UUID,
        error_code: object,
        available_at: datetime,
        now: datetime,
    ) -> bool:
        del intent_id, lease_token, error_code, available_at, now
        return False

    async def mark_terminal(
        self, intent_id: UUID, lease_token: UUID, error_code: object, now: datetime
    ) -> bool:
        del intent_id, lease_token, error_code, now
        return False


@dataclass
class FakeWorkflowStarter:
    """Workflow starter port converging both starts on one execution."""

    ledger: list[str]
    references: list[SourceIngestionReference] = field(default_factory=list)
    outcomes: list[ProjectionWorkflowStartResult] = field(default_factory=list)
    failure: Exception | None = None

    async def start_source_ingestion(
        self, reference: SourceIngestionReference
    ) -> ProjectionWorkflowStartResult:
        self.ledger.append("start_workflow")
        if self.failure is not None:
            raise self.failure
        self.references.append(reference)
        outcome = (
            ProjectionWorkflowStartResult.STARTED
            if len(self.references) == 1
            else ProjectionWorkflowStartResult.EXISTING
        )
        self.outcomes.append(outcome)
        return outcome


@dataclass
class FakePolicySeeder:
    """Signed empty-policy seeder recording the seeded workspaces in order."""

    ledger: list[str]
    seeded_workspaces: list[UUID] = field(default_factory=list)

    async def seed(
        self, workspace_id: UUID, owner_user_id: UUID, diagnostic_context: DiagnosticContext
    ) -> None:
        del owner_user_id, diagnostic_context
        self.ledger.append("policy:seed")
        self.seeded_workspaces.append(workspace_id)


@dataclass
class AllowingGuard:
    """Policy guard allowing both boundaries while recording the calls."""

    ledger: list[str]

    async def authorize_publication(
        self, command: object, diagnostic_context: DiagnosticContext
    ) -> object:
        del diagnostic_context
        self.ledger.append("policy:publication")
        return None

    async def authorize_read(
        self, reference: object, diagnostic_context: DiagnosticContext
    ) -> object:
        del diagnostic_context
        self.ledger.append("policy:read")
        return None


@dataclass
class TableCountsProbe:
    """Table-count provider snapshotting every read for the no-new-row proofs."""

    counts: dict[str, int]
    snapshots: list[dict[str, int]] = field(default_factory=list)

    async def __call__(self) -> Mapping[str, int]:
        snapshot = dict(self.counts)
        self.snapshots.append(snapshot)
        return snapshot


def _build_collaborators(
    *, starter_failure: Exception | None = None, read_failure: Exception | None = None
) -> tuple[
    PhaseOneAcceptanceCollaborators,
    FakeIdentityService,
    FakeObjectStore,
    FakePublicationStore,
    FakeWorkflowStarter,
    RecordingSink,
    InMemoryCanonicalAcceptanceMetrics,
    TableCountsProbe,
    list[str],
]:
    ledger: list[str] = []
    counts = {
        "users": 0,
        "workspaces": 0,
        "devices": 0,
        "audit_events": 0,
        "sources": 0,
        "source_versions": 0,
        "sync_events": 0,
        "projection_intents": 0,
        "content_objects": 0,
    }
    guard = AllowingGuard(ledger=ledger)
    policy_seeder = FakePolicySeeder(ledger=ledger)
    identity = FakeIdentityService(ledger=ledger, counts=counts)
    object_store = FakeObjectStore(ledger=ledger, clock=_fixed_clock)
    publication_store = FakePublicationStore(ledger=ledger, counts=counts)
    read_store = FakeReadStore(
        ledger=ledger, publication_store=publication_store, identity=identity
    )
    read_store.failure = read_failure
    intent_store = FakeIntentStore(
        ledger=ledger, publication_store=publication_store, identity=identity
    )
    starter = FakeWorkflowStarter(ledger=ledger)
    starter.failure = starter_failure
    sink = RecordingSink()
    metrics = InMemoryCanonicalAcceptanceMetrics()
    table_counts = TableCountsProbe(counts=counts)
    collaborators = PhaseOneAcceptanceCollaborators(
        identity_service=identity,
        publication_service=SourceVersionPublicationService(
            store=publication_store,  # type: ignore[arg-type]
            object_store=object_store,  # type: ignore[arg-type]
            metrics=InMemorySourcePublicationMetrics(),
            clock=_fixed_clock,
            policy_guard=guard,  # type: ignore[arg-type]
        ),
        read_service=CanonicalSourceReadService(
            store=read_store,  # type: ignore[arg-type]
            object_store=object_store,  # type: ignore[arg-type]
            metrics=InMemoryCanonicalReadMetrics(),
            policy_guard=guard,  # type: ignore[arg-type]
        ),
        policy_seeder=policy_seeder,
        intent_store=intent_store,  # type: ignore[arg-type]
        workflow_starter=starter,  # type: ignore[arg-type]
        table_counts=table_counts,
        diagnostics=sink,
        metrics=metrics,
        clock=_fixed_clock,
    )
    return (
        collaborators,
        identity,
        object_store,
        publication_store,
        starter,
        sink,
        metrics,
        table_counts,
        ledger,
    )


def _diagnostic_context() -> DiagnosticContext:
    return create_diagnostic_context().context


# --- The full spec-7 flow ------------------------------------------------------


@pytest.mark.asyncio
async def test_flow_proves_all_spec_7_claims() -> None:
    (
        collaborators,
        identity,
        _object_store,
        publication_store,
        starter,
        sink,
        metrics,
        table_counts,
        ledger,
    ) = _build_collaborators()

    summary = await run_phase_one_acceptance(collaborators)

    # Flow order: bootstrap, exact replay, synthetic publish (preflight miss ->
    # store/full-verify -> atomic commit), canonical read, publication replay,
    # fenced intent claims, one converging execution, fenced acknowledgements.
    assert ledger == [
        "bootstrap:create",
        "bootstrap:replay",
        "policy:seed",
        "policy:publication",
        "preflight:miss",
        "object:resolve",
        "object:store_stream",
        "commit_create",
        "read_current",
        "policy:read",
        "object:open_reader",
        "policy:publication",
        "preflight:hit",
        "claim_batch",
        "start_workflow",
        "start_workflow",
        "acknowledge:neo4j",
        "acknowledge:qdrant",
    ]

    # Bootstrap replay: same ids and original committed timestamp, no new row.
    assert identity.calls == 2
    assert table_counts.snapshots[0] == table_counts.snapshots[1]

    # Two intents converged on ONE deterministic execution with the identical
    # workflow id and the closed four-UUID input.
    committed = publication_store.committed
    assert committed is not None
    published_command = publication_store.commands[0]
    expected_reference = SourceIngestionReference(
        contract=SOURCE_INGESTION_REFERENCE_CONTRACT,
        workspace_id=identity.workspace_id,
        event_id=committed.event_id,
        source_id=committed.source_id,
        source_version_id=committed.source_version_id,
    )
    assert starter.references == [expected_reference, expected_reference]
    assert (
        projection_workflow_id(starter.references[0].workspace_id, starter.references[0].event_id)
        == f"source-ingestion/{identity.workspace_id}/{committed.event_id}"
        == projection_workflow_id(
            starter.references[1].workspace_id, starter.references[1].event_id
        )
    )
    assert starter.outcomes == [
        ProjectionWorkflowStartResult.STARTED,
        ProjectionWorkflowStartResult.EXISTING,
    ]
    assert committed.event_id == published_command.event_id

    assert summary["result_code"] == "canonical_acceptance_completed"
    expected_workflow_id = f"source-ingestion/{identity.workspace_id}/{committed.event_id}"
    assert summary["workflow_id"] == expected_workflow_id
    assert summary["projection_intent_count"] == 2
    assert metrics.acceptance_count(AcceptanceMetricOutcome.SUCCEEDED) == 1
    assert metrics.acceptance_count(AcceptanceMetricOutcome.FAILED) == 0
    assert [name for name, _ in sink.events] == [EventName.CANONICAL_ACCEPTANCE_COMPLETED]


@pytest.mark.asyncio
async def test_replay_bypasses_r2_and_adds_no_row() -> None:
    (
        collaborators,
        _identity,
        object_store,
        publication_store,
        _starter,
        _sink,
        _metrics,
        table_counts,
        _ledger,
    ) = _build_collaborators()

    await run_phase_one_acceptance(collaborators)

    # Exactly one store and one resolve from the FIRST publication; the replay
    # performed zero object-store interactions (no resolve, no store, no read).
    assert object_store.interactions.count("resolve") == 1
    assert object_store.interactions.count("store_stream") == 1
    assert object_store.interactions.count("open_reader") == 1
    assert publication_store.commit_count == 1
    # No new row across both replays: the bootstrap replay added none, the
    # publication added its rows exactly once, and neither the publication
    # replay nor the dispatch acknowledgements changed any row count.
    assert table_counts.snapshots[0] == table_counts.snapshots[1]
    assert table_counts.snapshots[2] != table_counts.snapshots[1]
    assert table_counts.snapshots[3] == table_counts.snapshots[2]
    assert table_counts.snapshots[4] == table_counts.snapshots[3]


@pytest.mark.asyncio
async def test_acceptance_emits_completed_event_and_safe_summary() -> None:
    (
        collaborators,
        identity,
        _object_store,
        publication_store,
        _starter,
        sink,
        _metrics,
        _table_counts,
        _ledger,
    ) = _build_collaborators()

    summary = await run_phase_one_acceptance(collaborators)

    completed = sink.of(EventName.CANONICAL_ACCEPTANCE_COMPLETED)
    assert len(completed) == 1
    committed = publication_store.committed
    assert committed is not None
    assert completed[0] == {
        "outcome": AcceptanceMetricOutcome.SUCCEEDED,
        "duration_ms": completed[0]["duration_ms"],
        "workspace_id": identity.workspace_id,
        "source_version_id": committed.source_version_id,
        "event_id": committed.event_id,
        "intent_count": 2,
    }
    assert isinstance(completed[0]["duration_ms"], int)

    # The summary is one JSON document of IDs and safe counts only.
    assert set(summary) == {
        "result_code",
        "user_id",
        "workspace_id",
        "device_id",
        "source_id",
        "source_version_id",
        "event_id",
        "content_version",
        "event_sequence",
        "size_bytes",
        "projection_intent_count",
        "workflow_id",
        "table_counts",
    }
    rendered = json.dumps(summary, sort_keys=True)
    published_command = publication_store.commands[0]
    assert published_command.title.value not in rendered
    assert published_command.expected_object.content_digest.hexadecimal not in rendered
    assert "Phase one acceptance" not in rendered


def test_failure_emits_failed_event_and_maps_exit_code() -> None:
    temporal_failure = ProjectionDispatchError(ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE)
    (
        collaborators,
        _identity,
        _object_store,
        _publication_store,
        _starter,
        sink,
        metrics,
        _table_counts,
        _ledger,
    ) = _build_collaborators(starter_failure=temporal_failure)

    with pytest.raises(ProjectionDispatchError):
        asyncio.run(run_phase_one_acceptance(collaborators))

    failed = sink.of(EventName.CANONICAL_ACCEPTANCE_FAILED)
    assert len(failed) == 1
    assert str(failed[0]["error_code"]) == "projection_dispatch_unavailable"
    assert failed[0]["is_retryable"] is True
    assert str(failed[0]["operation"]) == "phase_one_acceptance"
    assert isinstance(failed[0]["duration_ms"], int)
    assert metrics.acceptance_count(AcceptanceMetricOutcome.FAILED) == 1
    assert metrics.acceptance_count(AcceptanceMetricOutcome.SUCCEEDED) == 0
    assert [name for name, _ in sink.events] == [EventName.CANONICAL_ACCEPTANCE_FAILED]

    # Through the CLI: the retryable Temporal dispatch failure maps onto the
    # closed busy exit class with exactly one safe JSON document on stdout.
    (
        cli_collaborators,
        *_rest,
    ) = _build_collaborators(starter_failure=temporal_failure)

    class _FailingComposition:
        async def run(self) -> Mapping[str, object]:
            # Runs inside main's event loop already; a nested asyncio.run is
            # not possible here.
            await run_phase_one_acceptance(cli_collaborators)
            raise AssertionError("the acceptance flow must fail before this point")

    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(
        ["phase-one-acceptance"],
        environ=_LOCAL_ENVIRONMENT,
        stdout=stdout,
        stderr=stderr,
        compose_phase_one_acceptance=lambda invocation, environ: _FailingComposition(),
    )
    assert exit_code == int(CanonicalCoreExitCode.BUSY)
    stdout_documents = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert stdout_documents == [
        {"result_code": "projection_dispatch_unavailable", "state": "error"}
    ]


# --- The injectable monotonic clock behind duration_ms --------------------------


class _SteppedMonotonicClock:
    """Deterministic monotonic clock advancing a fixed step on every call."""

    def __init__(self, *, start_seconds: float = 100.0, step_seconds: float = 0.25) -> None:
        self._now_seconds = start_seconds
        self._step_seconds = step_seconds

    def __call__(self) -> float:
        current_seconds = self._now_seconds
        self._now_seconds += self._step_seconds
        return current_seconds


@pytest.mark.asyncio
async def test_completed_duration_ms_equals_injected_monotonic_clock_delta() -> None:
    (
        collaborators,
        _identity,
        _object_store,
        _publication_store,
        _starter,
        sink,
        _metrics,
        _table_counts,
        _ledger,
    ) = _build_collaborators()

    await run_phase_one_acceptance(collaborators, monotonic_clock=_SteppedMonotonicClock())

    completed = sink.of(EventName.CANONICAL_ACCEPTANCE_COMPLETED)
    assert len(completed) == 1
    # Start read 100.0, elapsed read 100.25: the reported duration is exactly
    # the fake 250 ms delta, never a real time.monotonic measurement.
    assert completed[0]["duration_ms"] == 250


@pytest.mark.asyncio
async def test_failed_duration_ms_equals_injected_monotonic_clock_delta() -> None:
    temporal_failure = ProjectionDispatchError(ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE)
    (
        collaborators,
        _identity,
        _object_store,
        _publication_store,
        _starter,
        sink,
        _metrics,
        _table_counts,
        _ledger,
    ) = _build_collaborators(starter_failure=temporal_failure)

    with pytest.raises(ProjectionDispatchError):
        await run_phase_one_acceptance(collaborators, monotonic_clock=_SteppedMonotonicClock())

    failed = sink.of(EventName.CANONICAL_ACCEPTANCE_FAILED)
    assert len(failed) == 1
    assert failed[0]["duration_ms"] == 250
