"""Composed-runtime regression: the service is the sole committed-metrics emitter.

Production passes one lifecycle recorder into the composed source lifecycle
runtime. Before the sole-emitter fix the durable store also recorded the
closed ``committed`` outcome inside its own commit, so one fresh commit
surfaced two committed rows in the recorder the Web Admin lifecycle
diagnostics route reads. These tests compose the real service over a store
fake shaped like that old durable store — it exposes the optional metrics
seam and records ``committed`` inside commit whenever the seam is wired —
and pin:
- the composed graph wires the recorder into the service only: one fresh
  commit records exactly one committed row, one exact replay records
  exactly one replayed row and no committed row;
- the detector has teeth: wiring the same recorder into the recording
  store doubles the committed rows, so any future double-emission fails
  the exact-count assertions above.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from tests.unit.source_lifecycle.fakes import (
    CallLedger,
    FakeLifecycleConflictCaptureGateway,
    FakeLifecyclePolicy,
    FakeLifecycleStore,
    build_commit_result,
    build_decision,
    build_device_context,
    build_diagnostic_context,
    build_rename_command,
)

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.source_lifecycle.commands import (
    LifecycleOperation,
    SourceLifecycleCommand,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.fingerprint import LifecycleRequestFingerprint
from personal_os.source_lifecycle.metrics import (
    InMemorySourceLifecycleMetrics,
    LifecycleMetricOutcome,
    SourceLifecycleMetricRecord,
    SourceLifecycleMetrics,
)
from personal_os.source_lifecycle.ports import (
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
)
from personal_os.source_lifecycle.service import SourceLifecycleService


@dataclass
class LegacyRecordingLifecycleStore(FakeLifecycleStore):
    """Store fake shaped like the pre-fix durable store.

    ``metrics`` is the optional seam the old
    ``PostgresqlSourceLifecycleStore`` constructor exposed: when wired, the
    store itself records the closed ``committed`` outcome inside commit —
    the double-emission the sole-emitter fix removed. The composition
    contract under test is that no graph wires this seam anymore; the
    durable store itself no longer accepts it.
    """

    metrics: SourceLifecycleMetrics | None = None

    async def commit(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
    ) -> SourceLifecycleCommitResult:
        result = await super().commit(
            command,
            device_context,
            request_fingerprint,
            policy_decision,
            diagnostic_context,
        )
        if self.metrics is not None:
            self.metrics.record_commit(
                operation=command.operation,
                outcome=LifecycleMetricOutcome.COMMITTED,
                duration_seconds=0.0,
            )
        return result


def _committed_rows(recorder: InMemorySourceLifecycleMetrics) -> list[SourceLifecycleMetricRecord]:
    return [
        record
        for record in recorder.commit_records()
        if record.outcome is LifecycleMetricOutcome.COMMITTED
    ]


@pytest.mark.asyncio
async def test_fresh_commit_records_exactly_one_committed_row_in_the_shared_recorder() -> None:
    """One fresh commit through the composed graph records exactly one committed row."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(
        device_context=device_context,
        command=command,
        outcome=LifecyclePolicyOutcome.ALLOWED,
    )
    commit_result = build_commit_result(command)
    ledger = CallLedger()
    recorder = InMemorySourceLifecycleMetrics()
    store = LegacyRecordingLifecycleStore(ledger=ledger, commit_result=commit_result)
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = SourceLifecycleService(
        store=store,
        policy=policy,
        conflict_capture=FakeLifecycleConflictCaptureGateway(ledger=ledger),
        metrics=recorder,
    )

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is commit_result
    committed_rows = _committed_rows(recorder)
    assert len(committed_rows) == 1
    assert committed_rows[0].operation is LifecycleOperation.RENAME
    assert (
        recorder.lifecycle_diagnostics().commit_counters[
            (LifecycleOperation.RENAME, LifecycleMetricOutcome.COMMITTED)
        ]
        == 1
    )


@pytest.mark.asyncio
async def test_exact_replay_records_exactly_one_replayed_row_and_no_committed_row() -> None:
    """One exact replay through the composed graph records exactly one replayed row."""

    command = build_rename_command()
    committed = build_commit_result(command)
    device_context = build_device_context()
    ledger = CallLedger()
    recorder = InMemorySourceLifecycleMetrics()
    store = LegacyRecordingLifecycleStore(
        ledger=ledger,
        commit_result=committed,
        committed_result=committed,
    )
    policy = FakeLifecyclePolicy(ledger=ledger)
    service = SourceLifecycleService(
        store=store,
        policy=policy,
        conflict_capture=FakeLifecycleConflictCaptureGateway(ledger=ledger),
        metrics=recorder,
    )

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is committed
    replayed_rows = [
        record
        for record in recorder.commit_records()
        if record.outcome is LifecycleMetricOutcome.REPLAYED
    ]
    assert len(replayed_rows) == 1
    assert replayed_rows[0].operation is LifecycleOperation.RENAME
    assert _committed_rows(recorder) == []


@pytest.mark.asyncio
async def test_wiring_the_recorder_into_the_recording_store_doubles_the_committed_rows() -> None:
    """The exact-count assertions above have teeth: a store that records
    into the shared recorder surfaces two committed rows per fresh commit —
    the double-emission the composed graph must never produce."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(
        device_context=device_context,
        command=command,
        outcome=LifecyclePolicyOutcome.ALLOWED,
    )
    commit_result = build_commit_result(command)
    ledger = CallLedger()
    recorder = InMemorySourceLifecycleMetrics()
    store = LegacyRecordingLifecycleStore(
        ledger=ledger,
        commit_result=commit_result,
        metrics=recorder,
    )
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = SourceLifecycleService(
        store=store,
        policy=policy,
        conflict_capture=FakeLifecycleConflictCaptureGateway(ledger=ledger),
        metrics=recorder,
    )

    await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert len(_committed_rows(recorder)) == 2


def test_durable_store_constructor_no_longer_accepts_a_metrics_seam() -> None:
    """The durable store removed the metrics param, so no composition can
    re-wire the recorder into the store side."""

    from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier

    from postgresql_source_store.lifecycle_store import PostgresqlSourceLifecycleStore

    engine = object()  # construction opens no connection; only the signature matters
    with pytest.raises(TypeError):
        PostgresqlSourceLifecycleStore(  # type: ignore[call-arg]
            engine,  # type: ignore[arg-type]
            policy_verifier=TrustAnchorEd25519Verifier(),
            metrics=InMemorySourceLifecycleMetrics(),
        )
