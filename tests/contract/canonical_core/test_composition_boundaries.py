"""Composition-boundary contracts for the phase-one acceptance work (spec 18.2).

The acceptance composition is a repository-internal CLI concern: the core
domains stay provider-neutral, the production R2 adapter keeps no destructive
capability, the corruption drills live only beneath the live-test harness,
the CLI parses strictly before any settings I/O, no tool ever names a raw
database URL or password environment variable, the public API/MCP surfaces and
the Alembic graph stay untouched, the Temporal workflow contract keeps its
approved values, and the two new acceptance events plus the
``canonical_acceptance_total{outcome}`` metric contract are registered.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

CORE_DOMAIN_ROOTS = (
    REPO_ROOT / "src" / "personal_os" / "identity",
    REPO_ROOT / "src" / "personal_os" / "recovery",
    REPO_ROOT / "src" / "personal_os" / "sources",
)

#: Provider/driver/process modules a core domain must never import.
FORBIDDEN_PROVIDER_MODULES = (
    "sqlalchemy",
    "psycopg",
    "temporalio",
    "aiobotocore",
    "botocore",
    "subprocess",
)

#: Capability names the production R2 adapter must not expose.
DESTRUCTIVE_ADAPTER_ATTRIBUTES = (
    "delete_object",
    "delete_objects",
    "list_objects",
    "list_object_versions",
    "copy_object",
    "overwrite_object",
    "presign_object",
    "generate_presigned_url",
    "generate_presigned_post",
)

ACCEPTANCE_NAME_TOKENS = (
    "phase-one-acceptance",
    "phase_one_acceptance",
    "run_phase_one_acceptance",
)

_MIGRATION_REVISION_RE = re.compile(r"(?m)^revision(?::\s*str)?\s*=\s*[\"']([^\"']+)[\"']")


def _iter_source_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _imported_module_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            names.add(node.module.split(".", 1)[0])
    return names


def test_core_imports_no_provider_driver_or_process_package() -> None:
    for root in CORE_DOMAIN_ROOTS:
        for path in _iter_source_files(root):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            offenders = _imported_module_names(tree) & set(FORBIDDEN_PROVIDER_MODULES)
            assert not offenders, f"{path} imports forbidden provider modules: {offenders}"


def test_production_r2_adapter_exposes_no_destructive_capability() -> None:
    from r2_object_storage.adapter import R2S3ObjectStore

    exposed = {name for name in dir(R2S3ObjectStore) if not name.startswith("_")}
    destructive = [name for name in DESTRUCTIVE_ADAPTER_ATTRIBUTES if name in exposed]
    assert not destructive, f"R2S3ObjectStore must not expose destructive capability: {destructive}"
    # The production surface stays exactly the verified canonical port.
    for required in (
        "resolve_verified_object",
        "store_stream",
        "verify_existing_object",
        "open_verified_reader",
        "close",
    ):
        assert hasattr(R2S3ObjectStore, required), f"missing canonical port method {required}"


def test_corruption_capability_lives_only_in_test_harness() -> None:
    package_root = REPO_ROOT / "packages" / "r2-object-storage" / "src" / "r2_object_storage"
    # The Child 7 spec (6.4) narrows the phase-one ban for exactly one module:
    # multipart staging may remove one exact staging object in production. The
    # capability name is permitted ONLY in multipart.py, only as the typed SDK
    # protocol declaration and the single direct call of the exact-key staging
    # removal path — never as a quoted name, never through dynamic dispatch,
    # and never alongside a broad-cleanup capability.
    staging_module = package_root / "multipart.py"
    for path in _iter_source_files(package_root):
        source = path.read_text(encoding="utf-8")
        if path == staging_module:
            assert source.count("delete_object") == 2, (
                "multipart.py must name the exact-key staging removal operation "
                "exactly twice: the protocol declaration and the single direct call"
            )
            assert "async def delete_object(" in source, (
                "multipart.py must declare the staging removal operation on its SDK protocol"
            )
            assert source.count("self._client.delete_object(") == 1, (
                "multipart.py must invoke the staging removal operation exactly once"
            )
            assert '"delete_object"' not in source, (
                "multipart.py must not carry the staging removal operation as a quoted name"
            )
            assert "getattr" not in source, (
                "multipart.py must not reach any SDK operation through dynamic dispatch"
            )
            for broad in ("delete_objects", "list_objects", "list_object_versions", "copy_object"):
                assert broad not in source, f"{path} must not contain {broad!r}"
            # The multipart exception is scoped to the staging-removal name
            # only: the sibling corruption-capability bans still apply here.
            for harness_only in ("write_object_under_digest", "delete_exact_object"):
                assert harness_only not in source, f"{path} must not contain {harness_only!r}"
            continue
        for forbidden in ("delete_object", "write_object_under_digest", "delete_exact_object"):
            assert forbidden not in source, f"{path} must not contain {forbidden!r}"
    harness = (REPO_ROOT / "tests" / "integration" / "r2_object_storage" / "conftest.py").read_text(
        encoding="utf-8"
    )
    assert "async def write_object_under_digest" in harness
    assert "async def delete_exact_object" in harness


def test_cli_parses_before_settings_io() -> None:
    from tools.canonical_core_operations import CanonicalCoreExitCode, main

    class _ExplodingEnviron(Mapping[str, str]):
        """Environment mapping that fails the test on any read."""

        def __getitem__(self, key: str) -> NoReturn:
            raise AssertionError(f"settings read before parse: {key}")

        def get(self, key: str, default: str | None = None) -> NoReturn:
            raise AssertionError(f"settings read before parse: {key}")

        def __iter__(self) -> NoReturn:
            raise AssertionError("settings iterated before parse")

        def __len__(self) -> NoReturn:
            raise AssertionError("settings measured before parse")

    exit_code = main(
        ["phase-one-acceptance", "--not-a-flag"],
        environ=_ExplodingEnviron(),
    )
    assert exit_code == int(CanonicalCoreExitCode.CLI)


def test_no_database_url_or_pgpassword_in_canonical_core_tools() -> None:
    # Scoped to the canonical-core operations CLI: the acceptance composition
    # must never name a raw database URL or password environment variable
    # (credentials travel only through the frozen settings loaders' secret
    # files). The local-stack tool's in-container provisioning scripts are the
    # pre-existing, separately contracted exception.
    for name in ("canonical_core_operations.py", "canonical_recovery_bundle.py"):
        source = (REPO_ROOT / "tools" / name).read_text(encoding="utf-8")
        assert "DATABASE_URL" not in source, f"tools/{name} names a raw database URL variable"
        assert "PGPASSWORD" not in source, f"tools/{name} names a raw password variable"


def test_no_public_api_mcp_openapi_change() -> None:
    for app_root in (REPO_ROOT / "apps" / "api" / "src", REPO_ROOT / "apps" / "mcp" / "src"):
        for path in _iter_source_files(app_root):
            source = path.read_text(encoding="utf-8")
            for token in ACCEPTANCE_NAME_TOKENS:
                assert token not in source, f"{path} references the internal acceptance CLI"
            assert "canonical_core_operations" not in source, (
                f"{path} imports the repository-internal tools composition"
            )


def test_no_new_alembic_revision() -> None:
    versions = [
        path
        for path in (REPO_ROOT / "migrations" / "versions").glob("*.py")
        if "__pycache__" not in path.parts
    ]
    revisions = {
        match
        for path in versions
        for match in _MIGRATION_REVISION_RE.findall(path.read_text(encoding="utf-8"))
    }
    assert revisions == {
        "20260813_01",
        "20260816_01",
        "20260817_01",
        "20260818_01",
        "20260820_01",
        "20260826_01",
        "20260826_02",
        "20260827_01",
        "20260828_01",
        "20260828_02",
        "20260828_03",
        "20260828_04",
        "20260829_01",
        "20260901_01",
        "20260901_02",
        "20260901_03",
        "20260902_01",
        "20260902_02",
    }, (
        f"the Alembic graph must stay exactly at the baseline, authentication, "
        f"exclusion policy, small-file sync, source-lifecycle, device sync, "
        f"manifest-run client-activity, multipart upload, submitted policy "
        f"verdict, grant-poll bucket kind, device-sync scale index, terminal "
        f"locator remediation, source-conflict and dismissal-retirement "
        f"revisions, got {sorted(revisions)}"
    )


def test_workflow_contract_unchanged() -> None:
    from workflow_worker.projection_workflow_starter import (
        PROJECTION_WORKFLOW_ID_PREFIX,
        PROJECTION_WORKFLOW_TASK_QUEUE,
        PROJECTION_WORKFLOW_TYPE_NAME,
        SOURCE_INGESTION_REFERENCE_CONTRACT,
        SourceIngestionReference,
    )

    assert PROJECTION_WORKFLOW_TYPE_NAME == "SourceIngestionWorkflow"
    assert PROJECTION_WORKFLOW_TASK_QUEUE == "source-ingestion"
    assert PROJECTION_WORKFLOW_ID_PREFIX == "source-ingestion"
    assert SOURCE_INGESTION_REFERENCE_CONTRACT == "source_ingestion_reference/v1"
    # The closed four-UUID input: the contract tag plus exactly the four entity
    # UUIDs and nothing else.
    assert [field.name for field in fields(SourceIngestionReference)] == [
        "contract",
        "workspace_id",
        "event_id",
        "source_id",
        "source_version_id",
    ]


def test_new_events_and_metrics_registered() -> None:
    from personal_os.diagnostics.events import (
        EVENT_DEFINITIONS,
        DiagnosticLevel,
        EventName,
        ResultCode,
    )
    from personal_os.recovery.contracts import (
        CANONICAL_ACCEPTANCE_METRIC_CONTRACTS,
        AcceptanceMetricOutcome,
        CanonicalAcceptanceMetrics,
        InMemoryCanonicalAcceptanceMetrics,
    )

    completed = EVENT_DEFINITIONS[EventName.CANONICAL_ACCEPTANCE_COMPLETED]
    assert completed.level is DiagnosticLevel.INFO
    assert completed.result_code is ResultCode.SUCCEEDED
    assert "duration_ms" in completed.required_fields
    failed = EVENT_DEFINITIONS[EventName.CANONICAL_ACCEPTANCE_FAILED]
    assert failed.level is DiagnosticLevel.ERROR
    assert failed.result_code is ResultCode.FAILED
    assert failed.required_fields == frozenset({"error_code"})

    assert dict(CANONICAL_ACCEPTANCE_METRIC_CONTRACTS) == {
        "canonical_acceptance_total": frozenset({"outcome"})
    }
    assert {outcome.value for outcome in AcceptanceMetricOutcome} == {"succeeded", "failed"}
    sink = InMemoryCanonicalAcceptanceMetrics()
    assert isinstance(sink, CanonicalAcceptanceMetrics)
    sink.record_acceptance(outcome=AcceptanceMetricOutcome.SUCCEEDED)
    assert sink.acceptance_count(AcceptanceMetricOutcome.SUCCEEDED) == 1


def test_acceptance_composition_lives_only_in_tools() -> None:
    # The worker adapter is consumable by tools, but tools is the only
    # composition root binding the acceptance flow together (spec 4.2).
    worker_root = REPO_ROOT / "apps" / "worker" / "src" / "workflow_worker"
    for path in _iter_source_files(worker_root):
        source = path.read_text(encoding="utf-8")
        assert "canonical_core_operations" not in source, f"{path} imports the tools composition"
        for token in ACCEPTANCE_NAME_TOKENS:
            assert token not in source, f"{path} references the acceptance composition"


def test_cli_composition_binds_a_diagnostics_sink_into_every_core_service() -> None:
    # Every construction of the three diagnostic-emitting canonical core
    # services inside the CLI composition must pass a `diagnostics=` sink so
    # the built and validated events are delivered, never silently discarded
    # by the composition.
    composed_services = {
        "IdentityBootstrapService",
        "CanonicalSourceReadService",
        "RecoveryService",
    }
    source_path = REPO_ROOT / "tools" / "canonical_core_operations.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in composed_services
    ]
    assert constructions, "the CLI composition must construct the canonical core services"
    for call in constructions:
        assert isinstance(call.func, ast.Name)
        keyword_names = [keyword.arg for keyword in call.keywords]
        assert "diagnostics" in keyword_names, (
            f"{call.func.id} construction at line {call.lineno} binds no diagnostics sink"
        )
