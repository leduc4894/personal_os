"""Sanitized reference-device gate for Child 5 live acceptance.

The Desktop WDIO journey and the physical Mobile matrix are mandatory live
evidence.  This contract validates only the operator record stored in the
living operations guide; it cannot replace either observation.  Placeholder,
failed, incomplete, future-dated, or privacy-unsafe records remain a hard
failure so Child 5 cannot be closed from automated evidence alone.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
RECORDS_PATH: Final[Path] = (
    REPO_ROOT / "docs" / "operations" / "source-locator-tombstone-lifecycle.md"
)

pytestmark = pytest.mark.device_records

REQUIRED_METADATA_LABELS: Final[tuple[str, ...]] = (
    "Device",
    "App version",
    "Plugin version",
    "Recorded at UTC",
    "Operator",
)
DESKTOP_SCENARIOS: Final[tuple[str, ...]] = (
    "Tracked rename",
    "Tracked move",
    "Delete",
    "Explicit restore",
    "Stable source and version identity",
    "Pending lifecycle drain",
)
MOBILE_SCENARIOS: Final[tuple[str, ...]] = (
    "Tracked rename",
    "Tracked move",
    "Delete",
    "Proven automatic restore",
    "Explicit restore",
    "Offline capture and reconnect",
    "Unload and reload",
    "Policy-denied transition",
)

_SECTION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^## (?P<device_class>Desktop|Mobile) live acceptance record$", re.MULTILINE
)
_METADATA_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^- (?P<label>[^:]+): (?P<value>.+)$", re.MULTILINE
)
_TABLE_ROW_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\| (?P<scenario>[^|]+?) \| (?P<outcome>[^|]+?) \| "
    r"(?P<evidence>[^|]+?) \|$",
    re.MULTILINE,
)
_UTC_TIMESTAMP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)
_FORBIDDEN_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {"", "pending", "not recorded", "todo", "n/a", "unknown", "unavailable"}
)
_PRIVACY_SENTINELS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~-]+"),
    re.compile(r"(?i)(?:access|refresh|polling)[_-]?token\s*[:=]"),
    re.compile(r"(?i)(?:expected|target|retained|normalized)[_-]?locator\s*[:=]"),
    re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", re.IGNORECASE),
    re.compile(r"(?i)(?:vault|note|file)[_-]?content\s*[:=]"),
)


def _sections(markdown: str) -> dict[str, str]:
    headings = list(_SECTION_PATTERN.finditer(markdown))
    sections: dict[str, str] = {}
    for index, match in enumerate(headings):
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        sections[match.group("device_class")] = markdown[start:end].strip()
    return sections


def _metadata(section_body: str) -> dict[str, str]:
    return {
        match.group("label").strip(): match.group("value").strip()
        for match in _METADATA_PATTERN.finditer(section_body)
    }


def _scenario_rows(section_body: str) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for match in _TABLE_ROW_PATTERN.finditer(section_body):
        scenario = match.group("scenario").strip()
        if scenario in {"Scenario", "---"}:
            continue
        rows[scenario] = (
            match.group("outcome").strip(),
            match.group("evidence").strip(),
        )
    return rows


def _assert_observed_metadata(device_class: str, section_body: str) -> None:
    metadata = _metadata(section_body)
    missing = [label for label in REQUIRED_METADATA_LABELS if label not in metadata]
    assert not missing, f"the {device_class} record is missing metadata: {missing}"
    placeholders = [
        label
        for label in REQUIRED_METADATA_LABELS
        if metadata[label].strip().lower() in _FORBIDDEN_PLACEHOLDERS
    ]
    assert not placeholders, (
        f"the {device_class} record carries placeholder metadata: {placeholders}"
    )
    recorded_text = metadata["Recorded at UTC"]
    assert _UTC_TIMESTAMP_PATTERN.fullmatch(recorded_text), (
        f"the {device_class} record needs an exact YYYY-MM-DDTHH:MM:SSZ UTC timestamp"
    )
    recorded_at = datetime.strptime(recorded_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert recorded_at <= datetime.now(tz=UTC), (
        f"the {device_class} record timestamp is in the future"
    )


def _assert_observed_scenarios(
    device_class: str,
    section_body: str,
    required_scenarios: tuple[str, ...],
) -> None:
    rows = _scenario_rows(section_body)
    missing = [scenario for scenario in required_scenarios if scenario not in rows]
    assert not missing, f"the {device_class} record is missing scenarios: {missing}"
    non_passing = [
        scenario for scenario in required_scenarios if rows[scenario][0].strip().upper() != "PASS"
    ]
    assert not non_passing, (
        f"the {device_class} record has scenarios without observed PASS outcomes: {non_passing}"
    )
    placeholder_evidence = [
        scenario
        for scenario in required_scenarios
        if rows[scenario][1].strip().lower() in _FORBIDDEN_PLACEHOLDERS
    ]
    assert not placeholder_evidence, (
        f"the {device_class} record carries placeholder evidence references: {placeholder_evidence}"
    )


def test_reference_device_records_are_complete_passing_and_sanitized() -> None:
    assert RECORDS_PATH.is_file(), f"live acceptance records are missing: {RECORDS_PATH}"
    markdown = RECORDS_PATH.read_text(encoding="utf-8")
    sections = _sections(markdown)
    for device_class, required_scenarios in (
        ("Desktop", DESKTOP_SCENARIOS),
        ("Mobile", MOBILE_SCENARIOS),
    ):
        body = sections.get(device_class)
        assert body, (
            f"the '{device_class} live acceptance record' section is missing; "
            "mandatory live evidence cannot be deferred or inferred"
        )
        _assert_observed_metadata(device_class, body)
        _assert_observed_scenarios(device_class, body, required_scenarios)

    for sentinel in _PRIVACY_SENTINELS:
        assert sentinel.search("\n".join(sections.values())) is None, (
            "reference-device records contain a forbidden raw locator, content, "
            "digest, or credential shape"
        )
