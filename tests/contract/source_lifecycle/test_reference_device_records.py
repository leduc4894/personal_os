"""Sanitized reference-device gate for Child 5 live acceptance.

The Desktop WDIO journey remains mandatory live evidence.  Physical Mobile
evidence may be deferred only through the closed handoff/backlog contract in
AGENTS.md.  This contract validates operator records; it cannot replace an
observation or turn a deferred Mobile matrix into a PASS.
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
BACKLOG_PATH: Final[Path] = REPO_ROOT / "docs" / "handoff" / "BACKLOG.md"
HANDOFF_PATH: Final[Path] = (
    REPO_ROOT / "docs" / "handoff" / "2026-08-20-source-locator-and-tombstone-lifecycle.md"
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
MOBILE_DEFERRAL_BACKLOG_KEY: Final = "source-lifecycle-mobile-acceptance"
MOBILE_DEFERRAL_HANDOFF_REFERENCE: Final = "handoff:source-lifecycle-mobile-deferral"
MOBILE_DEFERRAL_TRIGGER: Final = "Before Child 6 acceptance closure"
MOBILE_DEFERRAL_METADATA_LABELS: Final[tuple[str, ...]] = (
    "Status",
    "Reason",
    "Source handoff",
    "Backlog key",
    "Implement by",
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
_SAFE_EVIDENCE_REFERENCE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:handoff|operator-record):[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"
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
    unsafe_evidence = [
        scenario
        for scenario in required_scenarios
        if _SAFE_EVIDENCE_REFERENCE_PATTERN.fullmatch(rows[scenario][1]) is None
    ]
    assert not unsafe_evidence, (
        f"the {device_class} record requires a closed safe evidence reference for: "
        f"{unsafe_evidence}"
    )


def _assert_mobile_deferral(
    section_body: str,
    backlog_markdown: str,
    handoff_markdown: str,
) -> None:
    metadata = _metadata(section_body)
    missing = [label for label in MOBILE_DEFERRAL_METADATA_LABELS if label not in metadata]
    assert not missing, f"the Mobile deferral is missing metadata: {missing}"
    assert metadata["Status"] == "DEFERRED"
    assert metadata["Reason"].strip().lower() not in _FORBIDDEN_PLACEHOLDERS
    assert metadata["Source handoff"] == MOBILE_DEFERRAL_HANDOFF_REFERENCE
    assert metadata["Backlog key"] == MOBILE_DEFERRAL_BACKLOG_KEY
    assert metadata["Implement by"] == MOBILE_DEFERRAL_TRIGGER

    rows = _scenario_rows(section_body)
    missing_scenarios = [scenario for scenario in MOBILE_SCENARIOS if scenario not in rows]
    assert not missing_scenarios, f"the Mobile deferral is missing scenarios: {missing_scenarios}"
    invalid_scenarios = [
        scenario
        for scenario in MOBILE_SCENARIOS
        if rows[scenario] != ("DEFERRED", MOBILE_DEFERRAL_HANDOFF_REFERENCE)
    ]
    assert not invalid_scenarios, (
        "the Mobile record must use only the closed DEFERRED outcome and source "
        f"handoff reference: {invalid_scenarios}"
    )

    matching_backlog_rows = [
        line
        for line in backlog_markdown.splitlines()
        if line.startswith("|") and MOBILE_DEFERRAL_BACKLOG_KEY in line
    ]
    assert len(matching_backlog_rows) == 1, (
        "the Mobile deferral requires exactly one matching BACKLOG row"
    )
    backlog_cells = [cell.strip() for cell in matching_backlog_rows[0].split("|")[1:-1]]
    assert len(backlog_cells) == 5
    assert backlog_cells[3] == MOBILE_DEFERRAL_TRIGGER
    assert (
        "(2026-08-20-source-locator-and-tombstone-lifecycle.md#deferred-items)" in backlog_cells[4]
    )
    assert MOBILE_DEFERRAL_BACKLOG_KEY in handoff_markdown
    assert MOBILE_DEFERRAL_HANDOFF_REFERENCE in handoff_markdown


def _assert_sanitized_sections(sections: dict[str, str]) -> None:
    for sentinel in _PRIVACY_SENTINELS:
        assert sentinel.search("\n".join(sections.values())) is None, (
            "reference-device records contain a forbidden raw locator, content, "
            "digest, or credential shape"
        )


def test_desktop_reference_device_record_is_complete_passing_and_sanitized() -> None:
    assert RECORDS_PATH.is_file(), f"live acceptance records are missing: {RECORDS_PATH}"
    markdown = RECORDS_PATH.read_text(encoding="utf-8")
    sections = _sections(markdown)
    body = sections.get("Desktop")
    assert body, "the mandatory 'Desktop live acceptance record' section is missing"
    _assert_observed_metadata("Desktop", body)
    _assert_observed_scenarios("Desktop", body, DESKTOP_SCENARIOS)
    _assert_sanitized_sections(sections)


def test_mobile_reference_device_record_is_passing_or_closed_deferred() -> None:
    markdown = RECORDS_PATH.read_text(encoding="utf-8")
    sections = _sections(markdown)
    body = sections.get("Mobile")
    assert body, "the 'Mobile live acceptance record' section is missing"
    if _metadata(body).get("Status") == "DEFERRED":
        _assert_mobile_deferral(
            body,
            BACKLOG_PATH.read_text(encoding="utf-8"),
            HANDOFF_PATH.read_text(encoding="utf-8"),
        )
    else:
        _assert_observed_metadata("Mobile", body)
        _assert_observed_scenarios("Mobile", body, MOBILE_SCENARIOS)
    _assert_sanitized_sections(sections)


@pytest.mark.parametrize(
    "unsafe_evidence",
    (
        "private/meeting-notes.md",
        "confidential meeting transcript",
        "018f47a0-7b00-7000-8000-000000000042",
    ),
)
def test_reference_device_record_rejects_bare_sensitive_evidence(
    unsafe_evidence: str,
) -> None:
    section = "\n".join(
        f"| {scenario} | PASS | {unsafe_evidence} |" for scenario in DESKTOP_SCENARIOS
    )

    with pytest.raises(AssertionError, match="safe evidence reference"):
        _assert_observed_scenarios("Desktop", section, DESKTOP_SCENARIOS)
