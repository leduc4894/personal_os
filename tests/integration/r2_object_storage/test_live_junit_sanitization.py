"""Offline contract for the live R2 JUnit artifact sanitization boundary."""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from pathlib import Path

from tests.integration.r2_object_storage import conftest as live_conftest

_FAILURE_TOKEN = "provider-origin-sentinel.invalid"
_REQUEST_TOKEN = "request-header-sentinel"
_TRACEBACK_TOKEN = "provider-stack-frame-sentinel"
_PROPERTY_TOKEN = "provider-property-sentinel"


def test_sanitizer_removes_provider_failure_details_before_artifact_upload(
    tmp_path: Path,
) -> None:
    """Publishing raw failure fields, captured output or properties leaks a sentinel."""

    raw_report = tmp_path / "object-storage-live-raw.xml"
    sanitized_report = tmp_path / "object-storage-live.xml"
    raw_report.write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" errors="1" failures="1" skipped="0" tests="2" time="1.25">
    <properties>
      <property name="provider_request" value="{_PROPERTY_TOKEN}" />
    </properties>
    <testcase classname="live.contract" name="test_call_failure" time="0.50">
      <failure message="request to {_FAILURE_TOKEN} failed">
        {_TRACEBACK_TOKEN}: {_REQUEST_TOKEN}
      </failure>
      <system-out>captured {_FAILURE_TOKEN}</system-out>
    </testcase>
    <testcase classname="live.contract" name="test_setup_failure" time="0.75">
      <error message="setup request {_REQUEST_TOKEN} failed">{_TRACEBACK_TOKEN}</error>
      <system-err>captured {_PROPERTY_TOKEN}</system-err>
    </testcase>
  </testsuite>
</testsuites>
""",
        encoding="utf-8",
    )

    # Exercise the same harness command the workflow invokes.
    exit_code = live_conftest._run_harness_command(
        [
            "sanitize-junit",
            "--source",
            str(raw_report),
            "--destination",
            str(sanitized_report),
        ]
    )

    assert exit_code == 0
    sanitized = sanitized_report.read_text(encoding="utf-8")
    for forbidden in (
        _FAILURE_TOKEN,
        _REQUEST_TOKEN,
        _TRACEBACK_TOKEN,
        _PROPERTY_TOKEN,
    ):
        assert forbidden not in sanitized

    root = ElementTree.parse(sanitized_report).getroot()
    test_cases = root.findall(".//testcase")
    assert [(case.get("name"), case.get("time")) for case in test_cases] == [
        ("test_call_failure", "0.50"),
        ("test_setup_failure", "0.75"),
    ]
    failure = root.find(".//failure")
    error = root.find(".//error")
    assert failure is not None and error is not None
    assert failure.get("message") == "r2_live_failure_details_redacted"
    assert error.get("message") == "r2_live_failure_details_redacted"
    assert failure.text == "r2_live_failure_details_redacted"
    assert error.text == "r2_live_failure_details_redacted"
    assert root.find(".//properties") is None
    assert root.find(".//system-out") is None
    assert root.find(".//system-err") is None
