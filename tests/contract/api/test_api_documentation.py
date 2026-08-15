"""Documentation contract for the API runtime and generated-client surface.

The operator-facing documentation (root README, ``apps/api`` README and the
living operations guide) must keep naming the real commands, routes,
environment variables and gates exactly, and must state the production
OpenAPI policy in one unambiguous phrase. The canonical implementation plan
must track the Phase 2 child-spec sequence by semantic slug instead of
claiming the whole phase complete.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


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
