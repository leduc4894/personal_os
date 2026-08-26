"""Documentation contract for the API runtime and generated-client surface.

The operator-facing documentation (root README, ``apps/api`` README and the
living operations guide) must keep naming the real commands, routes,
environment variables and gates exactly, and must state the production
OpenAPI policy in one unambiguous phrase. The canonical implementation plan
must track the Phase 2 child-spec sequence by semantic slug instead of
claiming the whole phase complete.

The device-sync child 6 additions pin the canonical API and data-model
documentation against the implemented surface: every registered route with
its semantic operation id, the closed ``device_*`` error registry with its
HTTP statuses, the five manifest/cursor tables with their migration heads
and the accurate Child 5 closed / Child 6 implementation-complete status.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_MODEL_DOC = REPO_ROOT / "docs" / "07-POSTGRESQL_DATA_MODEL.md"
API_DOC = REPO_ROOT / "docs" / "12-API_MCP_AND_AGENT_INTEGRATION.md"

#: The eight registered device sync routes (spec 7.1-7.4) with their manually
#: assigned semantic operation ids. Documentation must name both members of
#: every pair, so a renamed route or operation id fails the docs gate.
DEVICE_SYNC_ROUTES_AND_OPERATION_IDS: tuple[tuple[str, str], ...] = (
    ("GET /api/sync/events", "pullDeviceSyncEvents"),
    ("POST /api/sync/cursor-acknowledgements", "acknowledgeDeviceSyncCursor"),
    ("POST /api/sync/manifests", "startDeviceManifest"),
    ("PUT /api/sync/manifests/{manifest_run_id}/pages/{page_number}", "appendDeviceManifestPage"),
    ("POST /api/sync/manifests/{manifest_run_id}/finalize", "finalizeDeviceManifest"),
    ("GET /api/sync/manifests/{manifest_run_id}/actions", "listDeviceManifestActions"),
    ("POST /api/sync/manifests/{manifest_run_id}/complete", "completeDeviceManifest"),
    (
        "GET /api/sources/{source_id}/versions/{source_version_id}/content",
        "downloadDeviceSourceVersion",
    ),
)

#: The closed device sync error registry (spec 13) with the HTTP status each
#: code maps to in the central status table. The one retryable dependency
#: outage is paired with its retryability marker so documentation cannot
#: present it as terminal.
DEVICE_SYNC_ERROR_CODE_STATUSES: tuple[tuple[str, str], ...] = (
    ("device_cursor_gap", "409"),
    ("device_cursor_regression", "409"),
    ("device_cursor_ack_ahead", "409"),
    ("device_event_unavailable", "404"),
    ("device_event_integrity_failed", "409"),
    ("device_manifest_not_found", "404"),
    ("device_manifest_expired", "410"),
    ("device_manifest_state_invalid", "409"),
    ("device_manifest_page_invalid", "422"),
    ("device_manifest_page_replay_mismatch", "409"),
    ("device_manifest_digest_mismatch", "422"),
    ("device_manifest_policy_advanced", "409"),
    ("device_download_integrity_failed", "422"),
    ("device_sync_dependency_unavailable", "503"),
)

#: The five device sync tables of the two 2026-08-26 migration heads.
DEVICE_SYNC_TABLES: tuple[str, ...] = (
    "device_cursors",
    "manifest_runs",
    "manifest_pages",
    "manifest_entry_resolutions",
    "manifest_actions",
)


def test_api_docs_name_exact_commands_routes_and_production_policy() -> None:
    root = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    api = (REPO_ROOT / "apps/api/README.md").read_text(encoding="utf-8")
    operations = (REPO_ROOT / "docs/operations/api-runtime-contract.md").read_text(encoding="utf-8")
    combined = root + api + operations
    for required in (
        "personal-api serve",
        "personal-api export-openapi",
        "/api/health/live",
        "/api/health/ready",
        "KNOWLEDGE_API_HOST",
        "KNOWLEDGE_API_PORT",
        "api-contract-check",
    ):
        assert required in combined
    assert "production OpenAPI is disabled" in combined


def test_implementation_plan_tracks_child_by_slug_not_phase_completion() -> None:
    plan = (REPO_ROOT / "docs/20-IMPLEMENTATION_PLAN.md").read_text(encoding="utf-8")
    assert "api-runtime-and-contract-foundation-design.md" in plan
    assert "docs/superpowers/plans/2026-08-15-api-runtime-and-contract-foundation.md" in plan
    assert "web-auth-and-device-authorization-design.md" in plan
    assert "Phase 2 sync is complete" not in plan


def test_api_doc_names_every_device_sync_route_and_operation_id() -> None:
    text = API_DOC.read_text(encoding="utf-8")
    missing = [
        f"{route} ({operation_id})"
        for route, operation_id in DEVICE_SYNC_ROUTES_AND_OPERATION_IDS
        if route not in text or operation_id not in text
    ]
    assert not missing, (
        "docs/12-API_MCP_AND_AGENT_INTEGRATION.md must name every registered "
        f"device sync route with its semantic operation id; missing: {missing}"
    )


def test_api_doc_registers_the_closed_device_sync_error_statuses() -> None:
    text = API_DOC.read_text(encoding="utf-8")
    offenders: list[str] = []
    for code, status in DEVICE_SYNC_ERROR_CODE_STATUSES:
        # Each code must be documented on a line that also carries its HTTP
        # status, so the registry cannot degrade to a bare token list.
        if not any(code in line and status in line for line in text.splitlines()):
            offenders.append(f"{code} paired with status {status}")
    assert not offenders, (
        "docs/12-API_MCP_AND_AGENT_INTEGRATION.md must document the closed "
        f"device_* error registry with each code's HTTP status; missing: {offenders}"
    )
    assert "device_sync_dependency_unavailable" in text and "retryable" in text, (
        "the API doc must mark device_sync_dependency_unavailable as the one "
        "retryable device sync code"
    )


def test_data_model_doc_names_device_sync_tables_and_migration_heads() -> None:
    text = DATA_MODEL_DOC.read_text(encoding="utf-8")
    missing = [table for table in DEVICE_SYNC_TABLES if table not in text]
    assert not missing, (
        "docs/07-POSTGRESQL_DATA_MODEL.md must name every device sync table of "
        f"the child 6 schema; missing: {missing}"
    )
    for head in ("20260826_01", "20260826_02"):
        assert head in text, f"docs/07-POSTGRESQL_DATA_MODEL.md must cite migration head {head}"
    assert "local_entry_id" in text, (
        "docs/07-POSTGRESQL_DATA_MODEL.md must record the 20260826_02 amendment: "
        "only the canonical-only download may lack a local entry echo"
    )


def test_data_model_doc_states_the_cursor_and_manifest_invariants() -> None:
    text = DATA_MODEL_DOC.read_text(encoding="utf-8")
    for anchor in (
        "one hour",
        "manifest completion",
        "sole exception",
        "hydrated at read time",
        "no locator text persists",
    ):
        assert anchor in text, (
            "docs/07-POSTGRESQL_DATA_MODEL.md must state the device sync "
            f"invariants; missing anchor: {anchor!r}"
        )


def test_implementation_plan_records_child_five_closed_with_live_evidence() -> None:
    plan = (REPO_ROOT / "docs/20-IMPLEMENTATION_PLAN.md").read_text(encoding="utf-8")
    assert "hoàn thành (2026-08-25)" in plan, (
        "docs/20-IMPLEMENTATION_PLAN.md must record Child 5 as closed on "
        "2026-08-25 using the same completion convention as Child 1-3"
    )
    assert "BLOCKED acceptance (2026-08-21)" not in plan, (
        "the stale Child 5 BLOCKED status must be corrected now that the "
        "Desktop WDIO journey and the physical Mobile matrix passed"
    )
    assert "Child 6 chưa được bắt đầu" not in plan, (
        "the plan must no longer claim Child 6 has not started"
    )


def test_implementation_plan_records_child_six_implementation_complete_not_closed() -> None:
    plan = (REPO_ROOT / "docs/20-IMPLEMENTATION_PLAN.md").read_text(encoding="utf-8")
    assert "device-cursor-and-manifest-reconciliation-design.md" in plan, (
        "docs/20-IMPLEMENTATION_PLAN.md must track Child 6 by its semantic slug"
    )
    child_six = re.search(
        r"Child 6 `device-cursor-and-manifest-reconciliation-design\.md`.*?(?=\n- Child|\n\n)",
        plan,
        re.DOTALL,
    )
    assert child_six is not None, "the plan must carry a Child 6 status bullet"
    bullet = child_six.group(0)
    assert "triển khai hoàn tất (2026-08-26), chờ live acceptance" in bullet, (
        "the Child 6 bullet must state implementation complete with live acceptance still pending"
    )
    assert "hoàn thành" not in bullet.replace("triển khai hoàn tất", ""), (
        "the Child 6 bullet must not claim the child complete while the "
        "Desktop WDIO journey and the physical Mobile matrix remain"
    )
    assert "Desktop WDIO" in bullet and "Mobile" in bullet, (
        "the Child 6 bullet must name the two remaining mandatory live gates"
    )
