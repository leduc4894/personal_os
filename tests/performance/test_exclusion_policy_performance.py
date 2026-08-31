"""Exclusion-policy performance gates on the reference host (spec 24).

The deterministic 10,000-source fixture and the 256-rule mixed workload pin
the four reference budgets: one subject against 256 rules evaluates in
p95 <= 5 ms, one maximum-size snapshot verifies in p95 <= 50 ms, the
10,000-subject preview reaches ready in <= 30 seconds and the
10,000-source reconciliation completes in <= 300 seconds without a
dependency outage. Preview and reconciliation run through the real store
code paths against the disposable PostgreSQL 18.4 stack (the same paged
500-row keyset scans the worker executes); the evaluator and verification
micro-benchmarks run the real domain code in-process.

Every budget records p50/p95/max wall times, and the module records the
reference-host evidence — platform, CPU, RAM, Python/PostgreSQL versions
and the live PostgreSQL capacity settings — before any assertion. Warmup
iterations are explicit and excluded from the measured samples. The suite
runs behind the ``local_stack`` marker and fails, never skips, when the
disposable stack is unavailable.
"""

from __future__ import annotations

import ctypes
import os
import platform as host_platform
import sys
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid5

import pytest
import pytest_asyncio
import sqlalchemy as sa
from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier
from pydantic import SecretStr
from tests.integration.exclusion_policy.conftest import (
    _APPLICATION_DATABASE,
    _APPLICATION_USER,
    _DATABASE_HOST,
    _SSL_MODE,
    PolicyMigrationHarness,
    PolicyMigrationStack,
    _assert_project_absent,
    _build_sanitized_environment,
    _read_application_password,
    _require_project_name,
    _resolved_host_port,
    _run_alembic,
    _run_stack_steps,
)
from tests.integration.exclusion_policy.test_source_publication_enforcement import (
    EnforcementHarness,
    _context,
)
from tools.local_service_stack import main as stack_main

from personal_os.exclusion_policy.contracts import (
    ExclusionPolicyRevision,
    PolicySubject,
    RuleKind,
)
from personal_os.exclusion_policy.evaluation import evaluate_policy
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.signatures import (
    SIGNED_SNAPSHOT_MAXIMUM_BYTES,
    SNAPSHOT_SIGNING_DOMAIN,
    build_signed_message,
    build_snapshot_payload,
    compute_payload_sha256_hex,
    compute_signed_snapshot_envelope_size,
)
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    derive_canonical_object_key,
)
from personal_os.sources.commands import SourceType
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.policy_drafts import PostgresqlPolicyDraftStore
from postgresql_source_store.policy_previews import PostgresqlPolicyPreviewStore
from postgresql_source_store.policy_reconciliation import (
    PostgresqlPolicyReconciliationStore,
)
from postgresql_source_store.settings import (
    DatabaseRuntimeSettings,
    load_database_runtime_settings,
)
from postgresql_source_store.tables import (
    content_objects,
    source_versions,
    sources,
    sync_events,
)

pytestmark = pytest.mark.local_stack


# --- the deterministic fixture constants ---------------------------------------------

#: The deterministic fixture size of spec 24.
SUBJECT_COUNT: Final[int] = 10_000

#: The maximum rule count per revision (spec 6).
RULE_COUNT: Final[int] = 256

#: Measured sample sizes; warmup iterations are explicit and excluded.
EVALUATOR_WARMUP_ITERATIONS: Final[int] = 20
EVALUATOR_SAMPLE_ITERATIONS: Final[int] = 300
VERIFY_WARMUP_ITERATIONS: Final[int] = 10
VERIFY_SAMPLE_ITERATIONS: Final[int] = 100

#: The four reference budgets of spec 24 (seconds).
EVALUATOR_P95_BUDGET_SECONDS: Final[float] = 0.005
VERIFY_P95_BUDGET_SECONDS: Final[float] = 0.050
PREVIEW_READY_BUDGET_SECONDS: Final[float] = 30.0
RECONCILIATION_BUDGET_SECONDS: Final[float] = 300.0

_FIXTURE_NAMESPACE: Final[UUID] = UUID("5f1e2d3c-0000-4000-8000-000000000001")

_SOURCE_TYPES: Final[tuple[SourceType, ...]] = (
    SourceType.MARKDOWN,
    SourceType.TEXT,
    SourceType.PDF,
    SourceType.IMAGE,
    SourceType.AUDIO,
    SourceType.WEB,
    SourceType.YOUTUBE,
)

_MEDIA_TYPES: Final[tuple[str, ...]] = (
    "text/markdown",
    "text/plain",
    "application/pdf",
    "image/png",
    "audio/mpeg",
    "text/html",
    "video/mp4",
)

#: Fixture sources whose index is divisible by this value carry no current
#: version, exercising the missing-evidence indeterminate path.
_EVIDENCE_DIVISOR: Final[int] = 5


def _fixture_source_id(index: int) -> UUID:
    return uuid5(_FIXTURE_NAMESPACE, f"perf-source-{index}")


def _build_mixed_rules() -> tuple[Any, ...]:
    """256 deterministic mixed rules covering all seven kinds (spec 6.2).

    Every rule carries a distinct semantic fingerprint: duplicate semantic
    rules are rejected (spec 6.3), so each kind's operands stay unique per
    rule. ``source_type`` admits only its seven-vocabulary values, so exactly
    one rule per source type is used; the remaining kinds split the rest with
    unique operands (``media_type`` rules beyond the seven subject types are
    syntactically valid types that simply never match).
    """
    unbounded_kinds: tuple[RuleKind, ...] = (
        RuleKind.EXACT_SOURCE_ID,
        RuleKind.FOLDER_PREFIX,
        RuleKind.PATH_GLOB,
        RuleKind.EXTENSION,
        RuleKind.MEDIA_TYPE,
        RuleKind.MAXIMUM_SIZE,
    )
    rules: list[Any] = []
    for index in range(RULE_COUNT):
        rule_id = uuid5(_FIXTURE_NAMESPACE, f"perf-rule-{index}")
        if index < len(_SOURCE_TYPES):
            rules.append(
                normalize_rule(
                    rule_id,
                    RuleKind.SOURCE_TYPE,
                    text_operand=_SOURCE_TYPES[index].value,
                )
            )
            continue
        kind = unbounded_kinds[(index - len(_SOURCE_TYPES)) % len(unbounded_kinds)]
        slot = (index - len(_SOURCE_TYPES)) // len(unbounded_kinds)
        if kind is RuleKind.EXACT_SOURCE_ID:
            rules.append(
                normalize_rule(rule_id, kind, source_id_operand=_fixture_source_id(slot * 6))
            )
        elif kind is RuleKind.FOLDER_PREFIX:
            rules.append(
                normalize_rule(rule_id, kind, text_operand=f"notes/perf-{slot:02d}/private")
            )
        elif kind is RuleKind.PATH_GLOB:
            rules.append(normalize_rule(rule_id, kind, text_operand=f"notes/perf-{slot:02d}/*"))
        elif kind is RuleKind.EXTENSION:
            rules.append(normalize_rule(rule_id, kind, text_operand=f".p{slot:02d}"))
        elif kind is RuleKind.MEDIA_TYPE:
            operand = _MEDIA_TYPES[slot] if slot < len(_MEDIA_TYPES) else f"text/x-perf-{slot:02d}"
            rules.append(normalize_rule(rule_id, kind, text_operand=operand))
        else:
            rules.append(normalize_rule(rule_id, kind, size_bytes_operand=1024 * (slot + 1)))
    assert len(rules) == RULE_COUNT
    fingerprints = {rule.semantic_fingerprint for rule in rules}
    assert len(fingerprints) == RULE_COUNT, "every mixed rule must stay semantically distinct"
    return tuple(rules)


# --- reference-host evidence -----------------------------------------------------------


def _total_ram_gib() -> float:
    """Best-effort total RAM reading; fails the gate when unavailable."""
    if sys.platform == "win32":

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return round(status.ullTotalPhys / (1024**3), 2)
    elif Path("/proc/meminfo").is_file():
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / (1024 * 1024), 2)
    pytest.fail("the reference-host RAM reading is unavailable; the budget evidence is incomplete")


def _percentile(samples: Sequence[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _summarize(label: str, samples: Sequence[float]) -> dict[str, float]:
    summary = {
        "p50_seconds": round(_percentile(samples, 0.50), 6),
        "p95_seconds": round(_percentile(samples, 0.95), 6),
        "max_seconds": round(max(samples), 6),
    }
    print(f"[budget] {label}: {summary}")
    return summary


def _summarize_wall(label: str, elapsed_seconds: float) -> dict[str, float]:
    summary = {
        "p50_seconds": round(elapsed_seconds, 3),
        "p95_seconds": round(elapsed_seconds, 3),
        "max_seconds": round(elapsed_seconds, 3),
    }
    print(f"[budget] {label}: {summary}")
    return summary


# --- the disposable stack and deterministic fixture ------------------------------------


@pytest.fixture(scope="module")
def performance_stack() -> Iterator[PolicyMigrationStack]:
    project_name = _require_project_name()
    port = _resolved_host_port()
    _run_stack_steps(project_name)
    upgraded = _run_alembic(_build_sanitized_environment(port), "upgrade", "head")
    assert upgraded.returncode == 0, "alembic upgrade head failed for the performance stack"
    try:
        yield _seed_performance_workspace(port, project_name)
    finally:
        try:
            stack_main(
                [
                    "reset",
                    "--project-name",
                    project_name,
                    "--confirm-project",
                    project_name,
                    "--non-interactive",
                ]
            )
        finally:
            _assert_project_absent(project_name)


def _seed_performance_workspace(port: int, project_name: str) -> PolicyMigrationStack:
    """Create the deterministic workspace graph and the 10,000-source fixture.

    Everything runs through one synchronous engine so the module's async
    harnesses see a complete deterministic database before any measurement.
    """
    environment = _build_sanitized_environment(port)
    settings: DatabaseRuntimeSettings = load_database_runtime_settings(environ=environment)
    password_text = _read_application_password()
    owner_user_id = uuid5(_FIXTURE_NAMESPACE, "perf-owner")
    workspace_id = uuid5(_FIXTURE_NAMESPACE, "perf-workspace")
    sync_engine = sa.create_engine(
        sa.URL.create(
            "postgresql+psycopg",
            username=_APPLICATION_USER,
            password=password_text,
            host=_DATABASE_HOST,
            port=port,
            database=_APPLICATION_DATABASE,
            query={"sslmode": _SSL_MODE},
        )
    )
    try:
        with sync_engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO knowledge.users (user_id, username, display_name)"
                    " VALUES (:user_id, :username, :display_name)"
                ),
                {
                    "user_id": owner_user_id,
                    "username": "performance-reference-owner",
                    "display_name": "Performance Reference Owner",
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO knowledge.workspaces"
                    " (workspace_id, owner_user_id, workspace_key, display_name)"
                    " VALUES (:workspace_id, :owner_user_id, :workspace_key, :display_name)"
                ),
                {
                    "workspace_id": workspace_id,
                    "owner_user_id": owner_user_id,
                    "workspace_key": "ws-perf-reference",
                    "display_name": "Performance Reference Workspace",
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO knowledge.workspace_policy_state"
                    " (workspace_id, active_policy_revision_id, active_revision_number)"
                    " VALUES (:workspace_id, NULL, 0)"
                ),
                {"workspace_id": workspace_id},
            )
            # The empty draft every workspace owns (acceptance 1): inserted
            # directly because this workspace is created after the migration
            # seeding pass.
            connection.execute(
                sa.text(
                    "INSERT INTO knowledge.policy_drafts"
                    " (policy_draft_id, workspace_id, draft_version,"
                    " base_policy_revision_id, created_by_user_id, updated_by_user_id)"
                    " VALUES (:policy_draft_id, :workspace_id, 1, NULL, :owner_user_id, NULL)"
                ),
                {
                    "policy_draft_id": uuid5(_FIXTURE_NAMESPACE, "perf-draft"),
                    "workspace_id": workspace_id,
                    "owner_user_id": owner_user_id,
                },
            )
        _seed_deterministic_fixture(sync_engine, workspace_id)
    finally:
        sync_engine.dispose()
    return PolicyMigrationStack(
        project_name=project_name,
        port=port,
        settings=settings,
        password=SecretStr(password_text),
        owner_user_id=owner_user_id,
        workspace_id=workspace_id,
        seeded_event_id=uuid5(_FIXTURE_NAMESPACE, "perf-seed-event"),
        seeded_source_id=_fixture_source_id(0),
        # The deterministic performance fixture has one committed version per
        # source, so immutable event evidence and the current pointer are the
        # same version for its seed source.  It does not stage the migration's
        # deliberately unbackfillable negative row.
        seeded_event_source_version_id=uuid5(_FIXTURE_NAMESPACE, "perf-version-0"),
        seeded_current_source_version_id=uuid5(_FIXTURE_NAMESPACE, "perf-version-0"),
        seeded_event_payload=b"",
        seeded_current_payload=b"",
        unbackfillable_upgrade_returncode=0,
        unbackfillable_upgrade_result_code="",
        revision_after_unbackfillable_upgrade="",
    )


def _seed_deterministic_fixture(sync_engine: sa.Engine, workspace_id: UUID) -> None:
    """Insert the deterministic 10,000-source fixture in bounded batches."""
    verified_at = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
    source_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    version_rows: list[dict[str, Any]] = []
    object_rows: list[dict[str, Any]] = []
    pointer_rows: list[dict[str, Any]] = []
    for index in range(SUBJECT_COUNT):
        source_id = _fixture_source_id(index)
        carries_evidence = index % _EVIDENCE_DIVISOR != 0
        version_id = uuid5(_FIXTURE_NAMESPACE, f"perf-version-{index}")
        content_object_id = uuid5(_FIXTURE_NAMESPACE, f"perf-object-{index}")
        source_rows.append(
            {
                "source_id": source_id,
                "workspace_id": workspace_id,
                "source_type": _SOURCE_TYPES[index % len(_SOURCE_TYPES)].value,
                "title": f"Performance reference source {index:05d}",
                "sync_state": "pending",
            }
        )
        if carries_evidence:
            object_rows.append(
                {
                    "content_object_id": content_object_id,
                    "content_hash": f"{index:064x}",
                    "object_key": derive_canonical_object_key(
                        ContentDigest.parse(f"{index:064x}")
                    ).value,
                    "byte_size": 1_024 + (index * 37) % 1_048_576,
                    "media_type": _MEDIA_TYPES[index % len(_MEDIA_TYPES)],
                    "verified_at": verified_at,
                    "created_at": verified_at,
                }
            )
            version_rows.append(
                {
                    "source_version_id": version_id,
                    "workspace_id": workspace_id,
                    "source_id": source_id,
                    "content_object_id": content_object_id,
                    "content_version": 1,
                    "author_kind": "user",
                    "author_id": uuid5(_FIXTURE_NAMESPACE, "perf-owner"),
                    "committed_at": verified_at,
                }
            )
            pointer_rows.append({"b_source_id": source_id, "b_version_id": version_id})
        event_rows.append(
            {
                "event_id": uuid5(_FIXTURE_NAMESPACE, f"perf-event-{index}"),
                "workspace_id": workspace_id,
                # event_sequence is GENERATED ALWAYS; the identity assigns it.
                "source_id": source_id,
                "idempotency_key": f"perf-event-{index:05d}",
                "request_fingerprint": f"{index:064x}",
                "event_type": "create",
                "committed_at": verified_at,
            }
        )
    pointer_update = (
        sa.update(sources)
        .values(
            sync_state="active",
            current_version_id=sa.bindparam("b_version_id"),
        )
        .where(sources.c.source_id == sa.bindparam("b_source_id"))
    )
    with sync_engine.begin() as connection:
        # sources and source_versions reference each other, so the fixture
        # lands in phases: bare pending sources, then the object, version and
        # event graph, then the current-pointer updates that activate the
        # evidence-carrying sources.
        for start in range(0, len(source_rows), 1_000):
            connection.execute(sa.insert(sources), source_rows[start : start + 1_000])
        for start in range(0, len(object_rows), 1_000):
            connection.execute(sa.insert(content_objects), object_rows[start : start + 1_000])
        for start in range(0, len(version_rows), 1_000):
            connection.execute(sa.insert(source_versions), version_rows[start : start + 1_000])
        for start in range(0, len(event_rows), 1_000):
            connection.execute(sa.insert(sync_events), event_rows[start : start + 1_000])
        for start in range(0, len(pointer_rows), 1_000):
            connection.execute(pointer_update, pointer_rows[start : start + 1_000])


@pytest.fixture(scope="module")
def performance_secret_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The module's one signing-key secret root.

    The module-scoped stack's database persists across the function-scoped
    harnesses, so every test must derive the same workspace signer from the
    same key file — a per-test directory would mint a new key that the
    already-initialized keyset refuses.
    """
    return tmp_path_factory.mktemp("exclusion-policy-performance-secrets")


@pytest_asyncio.fixture
async def performance_harness(
    performance_stack: PolicyMigrationStack,
    performance_secret_root: Path,
) -> Iterator[EnforcementHarness]:
    """One engine-bound harness per test (each async test owns its loop)."""
    engine = create_source_store_engine(performance_stack.settings, performance_stack.password)
    base = PolicyMigrationHarness(engine, performance_stack)
    harness = EnforcementHarness(base, performance_secret_root)
    await harness.ensure_keys_initialized()
    try:
        yield harness
    finally:
        await dispose_source_store_engine(engine)


def _evaluation_subject(index: int, workspace_id: UUID) -> PolicySubject:
    carries_evidence = index % _EVIDENCE_DIVISOR != 0
    return PolicySubject(
        workspace_id=workspace_id,
        source_id=_fixture_source_id(index),
        normalized_locator=None,
        source_type=_SOURCE_TYPES[index % len(_SOURCE_TYPES)],
        media_type=(
            CanonicalMediaType.parse(_MEDIA_TYPES[index % len(_MEDIA_TYPES)])
            if carries_evidence
            else None
        ),
        size_bytes=(1_024 + (index * 37) % 1_048_576) if carries_evidence else None,
    )


@pytest.fixture(scope="module")
def reference_host_evidence(performance_stack: PolicyMigrationStack) -> dict[str, Any]:
    """Record the reference-host evidence before any budget assertion."""
    sync_engine = sa.create_engine(
        sa.URL.create(
            "postgresql+psycopg",
            username=_APPLICATION_USER,
            password=_read_application_password(),
            host=_DATABASE_HOST,
            port=performance_stack.port,
            database=_APPLICATION_DATABASE,
            query={"sslmode": _SSL_MODE},
        )
    )
    try:
        with sync_engine.connect() as connection:
            version = connection.execute(sa.text("SELECT version()")).scalar_one()
            shared_buffers = connection.execute(sa.text("SHOW shared_buffers")).scalar_one()
            max_connections = connection.execute(sa.text("SHOW max_connections")).scalar_one()
            work_mem = connection.execute(sa.text("SHOW work_mem")).scalar_one()
    finally:
        sync_engine.dispose()
    evidence: dict[str, Any] = {
        "platform": host_platform.platform(),
        "processor": host_platform.processor() or host_platform.machine(),
        "cpu_count": os.cpu_count(),
        "total_ram_gib": _total_ram_gib(),
        "python_version": sys.version.split()[0],
        "postgresql": {
            "postgres_version": str(version).split(",")[0],
            "shared_buffers": str(shared_buffers),
            "max_connections": str(max_connections),
            "work_mem": str(work_mem),
        },
        "fixture_sources": SUBJECT_COUNT,
        "fixture_rules": RULE_COUNT,
    }
    print(f"[budget] reference host: {evidence}")
    return evidence


# --- the four budgets -------------------------------------------------------------------


def test_reference_host_evidence_is_recorded(reference_host_evidence: dict[str, Any]) -> None:
    """An unrecorded local observation cannot satisfy the gate (spec 24)."""
    for field in ("platform", "processor", "cpu_count", "total_ram_gib", "python_version"):
        assert reference_host_evidence[field], f"reference-host field '{field}' is unrecorded"
    postgres = reference_host_evidence["postgresql"]
    assert postgres["postgres_version"].startswith("PostgreSQL 18.4")
    for setting in ("shared_buffers", "max_connections", "work_mem"):
        assert postgres[setting]


@pytest.mark.asyncio
async def test_evaluator_p95_within_budget(
    performance_harness: EnforcementHarness,
    reference_host_evidence: dict[str, Any],
) -> None:
    """One subject against 256 rules: p95 <= 5 ms, warmup excluded."""
    del reference_host_evidence  # recorded by its fixture before any assertion
    rules = _build_mixed_rules()
    revision = ExclusionPolicyRevision(
        policy_revision_id=uuid5(_FIXTURE_NAMESPACE, "perf-revision-evaluator"),
        workspace_id=performance_harness.workspace_id,
        revision_number=1,
        rules=rules,
    )
    subjects = [_evaluation_subject(index, performance_harness.workspace_id) for index in range(30)]

    for index in range(EVALUATOR_WARMUP_ITERATIONS):  # explicit warmup, excluded
        evaluate_policy(revision=revision, subject=subjects[index % len(subjects)])

    samples: list[float] = []
    for iteration in range(EVALUATOR_SAMPLE_ITERATIONS):
        subject = subjects[iteration % len(subjects)]
        started = time.perf_counter()
        outcome = evaluate_policy(revision=revision, subject=subject)
        samples.append(time.perf_counter() - started)
        assert outcome.enforced is not None
    summary = _summarize("evaluator one-subject/256-rules", samples)
    assert summary["p95_seconds"] <= EVALUATOR_P95_BUDGET_SECONDS, (
        f"evaluator p95 {summary['p95_seconds']}s exceeds the 5 ms budget"
    )


@pytest.mark.asyncio
async def test_maximum_snapshot_verification_p95_within_budget(
    performance_harness: EnforcementHarness,
    reference_host_evidence: dict[str, Any],
) -> None:
    """One maximum-size snapshot verification: p95 <= 50 ms, warmup excluded."""
    del reference_host_evidence
    from personal_os.exclusion_policy.enforcement import (
        ActivePolicySnapshotMaterial,
        parse_verified_policy_revision,
    )

    rules = _build_mixed_rules()
    revision = ExclusionPolicyRevision(
        policy_revision_id=uuid5(_FIXTURE_NAMESPACE, "perf-revision-verify"),
        workspace_id=performance_harness.workspace_id,
        revision_number=1,
        rules=rules,
    )
    payload_bytes = build_snapshot_payload(
        revision,
        parent_policy_revision_id=None,
        published_at=datetime(2026, 8, 17, 0, 0, tzinfo=UTC),
    )
    envelope_size = compute_signed_snapshot_envelope_size(payload_bytes)
    assert envelope_size <= SIGNED_SNAPSHOT_MAXIMUM_BYTES
    assert envelope_size > 16 * 1024, "the verification fixture must be a large snapshot"
    signer = performance_harness.signing_key
    signature_bytes = signer.sign(build_signed_message(SNAPSHOT_SIGNING_DOMAIN, payload_bytes))
    material = ActivePolicySnapshotMaterial(
        workspace_id=performance_harness.workspace_id,
        policy_revision_id=revision.policy_revision_id,
        revision_number=1,
        payload_bytes=payload_bytes,
        payload_sha256=compute_payload_sha256_hex(payload_bytes),
        signature_bytes=signature_bytes,
        public_key_bytes=signer.public_key_bytes,
    )
    verifier = TrustAnchorEd25519Verifier()

    for _ in range(VERIFY_WARMUP_ITERATIONS):  # explicit warmup, excluded
        parse_verified_policy_revision(material, verifier=verifier)

    samples: list[float] = []
    for _ in range(VERIFY_SAMPLE_ITERATIONS):
        started = time.perf_counter()
        parsed = parse_verified_policy_revision(material, verifier=verifier)
        samples.append(time.perf_counter() - started)
        assert len(parsed.rules) == RULE_COUNT
    summary = _summarize("verify maximum-size snapshot", samples)
    assert summary["p95_seconds"] <= VERIFY_P95_BUDGET_SECONDS, (
        f"verification p95 {summary['p95_seconds']}s exceeds the 50 ms budget"
    )


@pytest.mark.asyncio
async def test_preview_ready_within_budget(
    performance_harness: EnforcementHarness,
    reference_host_evidence: dict[str, Any],
) -> None:
    """10,000 subjects against 256 mixed rules: ready <= 30 seconds."""
    del reference_host_evidence
    from personal_os.exclusion_policy.previews import PreviewStatus

    rules = _build_mixed_rules()
    draft_store = PostgresqlPolicyDraftStore(performance_harness.base.engine)
    preview_store = PostgresqlPolicyPreviewStore(performance_harness.base.engine)
    actor = performance_harness.actor()
    draft = await draft_store.load_draft(performance_harness.workspace_id, _context())
    await draft_store.replace_rules(draft.draft_id, draft.draft_version, rules, actor, _context())

    requested = await preview_store.request_preview(
        performance_harness.workspace_id, actor, _context()
    )
    started = time.perf_counter()
    record = await preview_store.run_preview_activity(requested.policy_preview_id, _context())
    elapsed = time.perf_counter() - started
    assert record.status is PreviewStatus.READY
    result_count = await preview_store.count_results(requested.policy_preview_id)
    assert result_count == SUBJECT_COUNT
    summary = _summarize_wall("preview 10000-subjects/256-rules", elapsed)
    assert summary["max_seconds"] <= PREVIEW_READY_BUDGET_SECONDS, (
        f"preview ready took {elapsed:.3f}s, exceeding the 30 s budget"
    )


@pytest.mark.asyncio
async def test_reconciliation_within_budget(
    performance_harness: EnforcementHarness,
    reference_host_evidence: dict[str, Any],
) -> None:
    """10,000-source reconciliation: <= 300 seconds without dependency outage."""
    del reference_host_evidence
    rules = _build_mixed_rules()
    revision_number = await performance_harness.publish_revision(*rules)
    assert revision_number == 1
    workspace_id = performance_harness.workspace_id
    revision_id = UUID(
        str(
            (
                await performance_harness.base.fetch_all(
                    "SELECT policy_revision_id FROM knowledge.source_policies"
                    " WHERE workspace_id = :workspace_id AND revision_number = 1",
                    {"workspace_id": workspace_id},
                )
            )[0][0]
        )
    )
    store = PostgresqlPolicyReconciliationStore(performance_harness.base.engine)
    started = time.perf_counter()
    evaluated_total = 0
    after_source_id: UUID | None = None
    for _ in range(100):
        outcome = await store.run_reconciliation_batch(
            workspace_id, revision_id, 0, after_source_id
        )
        evaluated_total += outcome.evaluated_sources
        if not outcome.has_more:
            break
        assert outcome.last_source_id is not None
        after_source_id = outcome.last_source_id
    else:
        pytest.fail("the reconciliation scan did not finish within the batch bound")
    elapsed = time.perf_counter() - started
    assert evaluated_total == SUBJECT_COUNT
    summary = _summarize_wall("reconciliation 10000-sources", elapsed)
    assert summary["max_seconds"] <= RECONCILIATION_BUDGET_SECONDS, (
        f"reconciliation took {elapsed:.3f}s, exceeding the 300 s budget"
    )
