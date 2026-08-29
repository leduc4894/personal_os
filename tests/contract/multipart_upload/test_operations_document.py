"""Multipart upload operations document and live-journey contract.

Task 13 of the resumable multipart mobile-upload child (spec 11 and 9.3):
the living operations runbook and the Desktop WDIO acceptance journey are
deliverables with their own static contract. The runbook must name the
exact staging key cleanup discipline and the physical Mobile gate, must
never teach a prefix-based delete, and must document every closed
``multipart_*`` reason token an operator can observe. The WDIO journey
must exist, must cover the four mandated scenarios (interruption/resume,
corruption refusal, policy advance, lost completion acknowledgement) and
must wire its closed phase codes through the guarded bootstrap mapping.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
RUNBOOK_PATH: Path = REPO_ROOT / "docs" / "operations" / "resumable-multipart-upload.md"
WDIO_SPEC_PATH: Path = (
    REPO_ROOT / "apps" / "obsidian-plugin" / "test" / "specs" / "multipart-upload.e2e.ts"
)
PHASE_STATUS_SUPPORT_PATH: Path = (
    REPO_ROOT / "apps" / "obsidian-plugin" / "test" / "support" / "live-acceptance-phase-status.ts"
)
BOOTSTRAP_TOOL_PATH: Path = REPO_ROOT / "tools" / "obsidian_live_acceptance_bootstrap.py"

#: Every closed ``multipart_*`` registry reason token (spec 7): the runbook
#: must name each one where an operator can read it.
CLOSED_MULTIPART_REASON_TOKENS: Final[tuple[str, ...]] = (
    "multipart_session_not_found",
    "multipart_session_expired",
    "multipart_session_state_invalid",
    "multipart_part_invalid",
    "multipart_part_url_rejected",
    "multipart_provider_state_invalid",
    "multipart_completion_in_progress",
    "multipart_integrity_failed",
    "multipart_policy_denied",
    "multipart_cleanup_failed",
    "multipart_local_content_changed",
    "multipart_dependency_unavailable",
)

#: The five recovery procedures the runbook owes an operator (plan task 13).
RECOVERY_PROCEDURE_HEADINGS: Final[tuple[str, ...]] = (
    "Safe resume",
    "Expiry",
    "Local content change",
    "Cleanup failure",
    "Re-auth",
)

#: The four mandated live scenarios plus the journey frame (spec 9.3).
WDIO_SCENARIO_MARKERS: Final[tuple[str, ...]] = (
    "interruption/resume",
    "corruption refusal",
    "policy advance",
    "lost completion acknowledgement",
)

#: The closed journey phase codes the WDIO journey reports through the
#: guarded bootstrap phase-status file.
WDIO_PHASE_RESULT_CODES: Final[tuple[str, ...]] = (
    "multipart_journey_started",
    "multipart_resume_committed",
    "multipart_corruption_refused",
    "multipart_lost_ack_replayed",
    "multipart_policy_denial_observed",
    "multipart_journey_completed",
)

#: Wire-material shapes the WDIO spec source must never print: a logged
#: presigned URL query, its signature, or a staging key prefix.
FORBIDDEN_WDIO_LOG_PATTERNS: Final[tuple[str, ...]] = (
    r"console\.\w+\([^)]*\bauthorization\.url\b",
    r"console\.\w+\([^)]*\burl\s*[,)+]",
    r"console\.\w+\([^)]*X-Amz-Signature",
    r"console\.\w+\([^)]*staging/multipart/",
)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- the runbook ------------------------------------------------------------------------------


def test_operations_runbook_names_exact_cleanup_and_mobile_gate() -> None:
    document = read_text(RUNBOOK_PATH)
    assert "exact staging key" in document
    assert "physical Mobile" in document
    assert "prefix delete" not in document


def test_operations_runbook_documents_every_closed_reason_token() -> None:
    document = read_text(RUNBOOK_PATH)
    for token in CLOSED_MULTIPART_REASON_TOKENS:
        assert token in document, token


def test_operations_runbook_documents_each_recovery_procedure() -> None:
    document = read_text(RUNBOOK_PATH)
    for heading in RECOVERY_PROCEDURE_HEADINGS:
        assert heading in document, heading


def test_operations_runbook_names_the_guarded_live_command_pair() -> None:
    document = read_text(RUNBOOK_PATH)
    assert "serve-live-ci.sh up" in document
    assert "serve-live-ci.sh down" in document
    assert "obsidian_live_acceptance_bootstrap.py" in document


# --- the Desktop WDIO acceptance journey ------------------------------------------


def test_wdio_journey_spec_exists_and_covers_every_mandated_scenario() -> None:
    document = read_text(WDIO_SPEC_PATH)
    for marker in WDIO_SCENARIO_MARKERS:
        assert marker in document, marker


def test_wdio_journey_reports_only_closed_phase_codes() -> None:
    document = read_text(WDIO_SPEC_PATH)
    for phase_code in WDIO_PHASE_RESULT_CODES:
        assert f'"{phase_code}"' in document, phase_code


def test_wdio_journey_never_logs_wire_transfer_material() -> None:
    document = read_text(WDIO_SPEC_PATH)
    for pattern in FORBIDDEN_WDIO_LOG_PATTERNS:
        assert re.search(pattern, document) is None, pattern


def test_phase_status_support_and_bootstrap_accept_the_journey_codes() -> None:
    support_document = read_text(PHASE_STATUS_SUPPORT_PATH)
    bootstrap_document = read_text(BOOTSTRAP_TOOL_PATH)
    for phase_code in WDIO_PHASE_RESULT_CODES:
        assert f'"{phase_code}"' in support_document, phase_code
        assert f'"{phase_code}"' in bootstrap_document, phase_code
    assert '"test/specs/multipart-upload.e2e.ts"' in bootstrap_document
