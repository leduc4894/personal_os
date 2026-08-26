"""Reference-device verification record gate (device-sync child 6, spec 18.1)
plus the child's documentation-contract checks.

Completion of the device cursor and manifest reconciliation child requires
recorded Desktop and Mobile Obsidian reference-device verification of the
live journeys the design's mandatory live gates name. These checks cannot
be automated in CI — they happen on physical reference devices against the
live local stack — so this gate validates the recorded evidence file instead
and stays out of the default marker expression (run it explicitly with
``-m device_records`` or ``uv run poe device-sync-device-verification``).

The evidence file is ``docs/operations/device-sync-device-verification.md``
with one ``## Desktop reference device`` and one ``## Mobile reference
device`` section; each section must carry its verification records as
``- <Label>: <observed outcome>`` entries plus a dated operator line. A
missing file, a missing section, a missing record or an empty outcome fails
the gate — absence of either record blocks the final child handoff, and no
placeholder may satisfy it.

The documentation-contract tests below the record gate carry NO marker:
they pin the living operator runbook and the plugin release candidate to
the implemented surface and run in every default suite. Only the physical
evidence gate is deliberately explicit.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
RECORDS_PATH: Final[Path] = REPO_ROOT / "docs" / "operations" / "device-sync-device-verification.md"
RUNBOOK: Final[Path] = (
    REPO_ROOT / "docs" / "operations" / "device-cursor-manifest-reconciliation.md"
)
PLUGIN_MANIFEST: Final[Path] = REPO_ROOT / "apps" / "obsidian-plugin" / "manifest.json"
PLUGIN_PACKAGE: Final[Path] = REPO_ROOT / "apps" / "obsidian-plugin" / "package.json"
PLUGIN_README: Final[Path] = REPO_ROOT / "apps" / "obsidian-plugin" / "README.md"

#: The live-gate verification records each reference-device section must
#: carry (design §18.1: the Desktop WDIO journeys and the physical Mobile
#: matrix — no mock, unit inference or Desktop evidence substitutes for the
#: Mobile rows).
REQUIRED_RECORD_LABELS_BY_DEVICE: Final[dict[str, tuple[str, ...]]] = {
    "Desktop": (
        "Remote edit no-echo",
        "Cursor gap to manifest repair",
        "Lost-SQLite recovery without duplicate sources",
        "Remote tombstone to local trash",
    ),
    "Mobile": (
        "Manifest suspend/resume",
        "Remote apply no-echo",
        "Lost-SQLite repair",
        "Tombstone to local trash",
        "Edit-during-reconciliation preservation",
    ),
}

_SECTION_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^## (?P<title>Desktop|Mobile) reference device$", re.MULTILINE
)
_RECORD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^- (?P<label>[^:]+): (?P<outcome>.+)$", re.MULTILINE
)
_DATED_OPERATOR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^Recorded by .+ on (?P<date>\d{4}-\d{2}-\d{2})\.$", re.MULTILINE
)

_PLACEHOLDER_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"", "pending", "not recorded", "TODO", "PENDING"}
)


def _sections(markdown: str) -> dict[str, str]:
    """Split the records file into its Desktop and Mobile section bodies."""

    headings = list(_SECTION_HEADING_PATTERN.finditer(markdown))
    found: dict[str, str] = {}
    for index, match in enumerate(headings):
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        found[match.group("title")] = markdown[start:end].strip()
    return found


def _records(section_body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _RECORD_PATTERN.finditer(section_body):
        found[match.group("label").strip()] = match.group("outcome").strip()
    return found


@pytest.mark.device_records
def test_reference_device_verification_records_are_present_and_complete() -> None:
    if not RECORDS_PATH.is_file():
        pytest.fail(
            "the reference-device verification records are missing: "
            f"{RECORDS_PATH} does not exist. Desktop and Mobile Obsidian "
            "reference-device verification of the design 18.1 live gates (remote "
            "edit no-echo, cursor gap to manifest repair, lost-SQLite recovery "
            "without duplicate sources, remote tombstone to local trash; manifest "
            "suspend/resume, remote apply no-echo, lost-SQLite repair, tombstone "
            "to local trash, edit-during-reconciliation preservation) must be "
            "recorded before the device-sync handoff; absence blocks completion."
        )
    sections = _sections(RECORDS_PATH.read_text(encoding="utf-8"))
    for device_class, required_labels in REQUIRED_RECORD_LABELS_BY_DEVICE.items():
        body = sections.get(device_class)
        if not body:
            pytest.fail(
                f"the '{device_class} reference device' section is missing from "
                f"{RECORDS_PATH}; both device classes must be recorded"
            )
        records = _records(body)
        missing = [label for label in required_labels if label not in records]
        assert not missing, f"the {device_class} section is missing verification records: {missing}"
        placeholders = [
            label for label in required_labels if records[label] in _PLACEHOLDER_OUTCOMES
        ]
        assert not placeholders, (
            f"the {device_class} records carry placeholder outcomes: {placeholders}; "
            "only observed outcomes satisfy the gate"
        )
        dated = _DATED_OPERATOR_PATTERN.search(body)
        assert dated is not None, (
            f"the {device_class} section needs a 'Recorded by <operator> on "
            "YYYY-MM-DD.' line naming the human who observed the device"
        )
        recorded_day = date.fromisoformat(dated.group("date"))
        assert recorded_day <= date.today(), (
            f"the {device_class} record date {recorded_day} is in the future"
        )


# --- the child 6 operator runbook documentation contract ----------------------


def test_device_sync_runbook_names_every_failure_surface() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for token in (
        "cursor_failure",
        "apply_failure",
        "reconcile_failure",
        "composition_read_failure",
    ):
        assert token in text


def test_device_sync_runbook_states_the_cadence_and_cursor_guarantees() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for anchor in (
        "every 30 seconds",
        "six hours",
        "one second to five minutes",
        "one hour",
        "terminal-safe",
        "sole exception",
        "catch-up burst",
    ):
        assert anchor in text, f"the runbook must state the cadence/cursor anchor {anchor!r}"


def test_device_sync_runbook_states_local_trash_and_identity_proof_rules() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "Vault.trash(file, false)" in text, (
        "the runbook must name the exact local-trash call of the remote delete path"
    )
    assert "no hard-delete fallback" in text, (
        "the runbook must state that remote delete has no hard-delete fallback"
    )
    for anchor in ("current locator", "historical locator", "open tombstone", "hash-only"):
        assert anchor in text, f"the runbook must state the identity proof rule {anchor!r}"
    assert "hash-only" in text and "never binds" in text


def test_device_sync_runbook_names_the_mandatory_live_gates_and_their_owner() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for anchor in ("Desktop WDIO", "physical Mobile", "device-sync-device-verification.md"):
        assert anchor in text, f"the runbook must name the live-gate anchor {anchor!r}"
    assert "not claim Child 6 complete" in text or "no completion claim" in text, (
        "the runbook must state that the live gates keep Child 6 open — no "
        "completion claim is permitted before they pass"
    )


def test_plugin_release_metadata_carries_the_child_six_feature_release() -> None:
    manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    package = json.loads(PLUGIN_PACKAGE.read_text(encoding="utf-8"))
    assert manifest["version"] == "0.2.0", (
        "the plugin manifest must carry the child 6 feature release 0.2.0"
    )
    assert package["version"] == "0.2.0", (
        "the plugin package version must move together with the manifest"
    )
    readme = PLUGIN_README.read_text(encoding="utf-8")
    for anchor in (
        "obsidian_sync_diagnostics_trail/v1",
        "obsidian_sync_diagnostics_trail/v2",
        "v6",
        "v7",
        "manifest repair",
    ):
        assert anchor in readme, (
            f"the plugin README must carry the release compatibility note; missing {anchor!r}"
        )
    assert not (REPO_ROOT / "apps" / "obsidian-plugin" / "versions.json").exists(), (
        "the repository has no versions.json release artifact and must not grow one"
    )
