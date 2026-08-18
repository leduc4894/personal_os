"""Reference-device verification record gate (spec 23.5/25 final handoff).

Completion of the exclusion-policy child requires recorded Desktop and
Mobile Obsidian reference-device verification of initial trust, snapshot
verification, rotation, offline cache and Vault preservation. These checks
cannot be automated in CI — they happen on physical reference devices — so
this gate validates the recorded evidence file instead and stays out of the
default marker expression (run it explicitly with ``-m device_records`` or
``uv run poe exclusion-policy-device-verification``).

The evidence file is ``docs/operations/exclusion-policy-device-verification.md``
with one ``## Desktop reference device`` and one ``## Mobile reference
device`` section; each section must carry the five verification records as
``- <Label>: <observed outcome>`` entries plus a dated operator line. A
missing file, a missing section, a missing record or an empty outcome fails
the gate — absence of either record blocks the final handoff, and no
placeholder may satisfy it.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
RECORDS_PATH: Final[Path] = (
    REPO_ROOT / "docs" / "operations" / "exclusion-policy-device-verification.md"
)

pytestmark = pytest.mark.device_records

#: The five verification records every reference-device section must carry.
REQUIRED_RECORD_LABELS: Final[tuple[str, ...]] = (
    "Initial trust",
    "Snapshot verification",
    "Rotation",
    "Offline cache",
    "Vault preservation",
)

_SECTION_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^## (?P<title>Desktop|Mobile) reference device$", re.MULTILINE
)
_RECORD_PATTERN: Final[re.Pattern[str]] = re.compile(r"^- (?P<label>[^:]+): (?P<outcome>.+)$")
_DATED_OPERATOR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^Recorded by .+ on (?P<date>\d{4}-\d{2}-\d{2})\.$", re.MULTILINE
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


def test_reference_device_verification_records_are_present_and_complete() -> None:
    if not RECORDS_PATH.is_file():
        pytest.fail(
            "the reference-device verification records are missing: "
            f"{RECORDS_PATH} does not exist. Desktop and Mobile Obsidian "
            "reference-device verification of initial trust, snapshot verification, "
            "rotation, offline cache and Vault preservation must be recorded before "
            "the exclusion-policy handoff; absence blocks completion."
        )
    sections = _sections(RECORDS_PATH.read_text(encoding="utf-8"))
    for device_class in ("Desktop", "Mobile"):
        body = sections.get(device_class)
        if not body:
            pytest.fail(
                f"the '{device_class} reference device' section is missing from "
                f"{RECORDS_PATH}; both device classes must be recorded"
            )
        records = _records(body)
        missing = [label for label in REQUIRED_RECORD_LABELS if label not in records]
        assert not missing, f"the {device_class} section is missing verification records: {missing}"
        empty = [
            label
            for label in REQUIRED_RECORD_LABELS
            if records[label] in {"", "pending", "not recorded", "TODO"}
        ]
        assert not empty, (
            f"the {device_class} records carry placeholder outcomes: {empty}; "
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
