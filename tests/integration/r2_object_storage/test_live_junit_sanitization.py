"""Offline contract for the live R2 JUnit artifact sanitization boundary."""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from tests.integration.r2_object_storage import conftest as live_conftest

_FAILURE_TOKEN = "provider-origin-sentinel.invalid"
_REQUEST_TOKEN = "request-header-sentinel"
_TRACEBACK_TOKEN = "provider-stack-frame-sentinel"
_PROPERTY_TOKEN = "provider-property-sentinel"
_ORDINARY_OUTPUT_TOKEN = "ordinary-system-out-sentinel"
_MALFORMED_RECORD_TOKEN = "malformed-diagnostic-sentinel"
_NESTED_FAILURE_TOKEN = "nested-provider-detail-sentinel"
_STREAM_ATTRIBUTE_TOKEN = "stream-attribute-sentinel"
_STREAM_DESCENDANT_TOKEN = "stream-descendant-sentinel"
_STREAM_TAIL_TOKEN = "stream-tail-sentinel"
_PASSING_OUTPUT_TOKEN = "passing-output-sentinel"


def _zero_byte_record(*, stage: str = "store", reason: str = "provider_timeout") -> str:
    return live_conftest.ZeroByteLiveDiagnostic(stage=stage, reason=reason).to_json()


def _record_with_fields(**fields: str) -> str:
    return json.dumps(fields, separators=(",", ":"), sort_keys=True)


def test_sanitizer_retains_only_one_valid_zero_byte_diagnostic_before_artifact_upload(
    tmp_path: Path,
) -> None:
    """Only the failed zero-byte case may retain Task 1's exact closed record."""

    raw_report = tmp_path / "object-storage-live-raw.xml"
    sanitized_report = tmp_path / "object-storage-live.xml"
    extra_field_record = _record_with_fields(
        event="r2_live_zero_byte_failed",
        stage="store",
        reason="provider_timeout",
        extra=_MALFORMED_RECORD_TOKEN,
    )
    wrong_stage_record = _record_with_fields(
        event="r2_live_zero_byte_failed", stage="upload", reason="provider_timeout"
    )
    wrong_reason_record = _record_with_fields(
        event="r2_live_zero_byte_failed", stage="store", reason=_MALFORMED_RECORD_TOKEN
    )
    raw_report.write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" errors="1" failures="9" skipped="0" tests="10" time="1.25">
    <properties>
      <property name="provider_request" value="{_PROPERTY_TOKEN}" />
    </properties>
    <testcase classname="live.contract" name="test_call_failure" time="0.50">
      <failure
        message="request to {_FAILURE_TOKEN} failed"
        provider_context="{_NESTED_FAILURE_TOKEN}"
      >
        {_TRACEBACK_TOKEN}: {_REQUEST_TOKEN}
        <provider-details request="{_NESTED_FAILURE_TOKEN}">
          {_NESTED_FAILURE_TOKEN}
        </provider-details>{_NESTED_FAILURE_TOKEN}
      </failure>
      <system-out>captured {_FAILURE_TOKEN} {_ORDINARY_OUTPUT_TOKEN}</system-out>
    </testcase>
    <testcase classname="live.contract" name="test_setup_failure" time="0.75">
      <error
        message="setup request {_REQUEST_TOKEN} failed"
        provider_context="{_NESTED_FAILURE_TOKEN}"
      >
        {_TRACEBACK_TOKEN}
        <provider-details request="{_NESTED_FAILURE_TOKEN}">
          {_NESTED_FAILURE_TOKEN}
        </provider-details>{_NESTED_FAILURE_TOKEN}
      </error>
      <system-err>captured {_PROPERTY_TOKEN}</system-err>
    </testcase>
    <testcase classname="live.r2" name="test_zero_byte_round_trip" time="0.10">
      <failure message="request to {_FAILURE_TOKEN} failed">{_TRACEBACK_TOKEN}</failure>
      <system-out provider_context="{_STREAM_ATTRIBUTE_TOKEN}">
        pytest captured stdout header
        {_zero_byte_record()}
        <provider-details request="{_STREAM_DESCENDANT_TOKEN}">
          {_STREAM_DESCENDANT_TOKEN}
        </provider-details>{_STREAM_TAIL_TOKEN}
      </system-out>{_STREAM_TAIL_TOKEN}
    </testcase>
    <testcase classname="live.r2" name="test_zero_byte_round_trip" time="0.10">
      <failure message="request to {_FAILURE_TOKEN} failed">{_TRACEBACK_TOKEN}</failure>
      <system-out>{_MALFORMED_RECORD_TOKEN}</system-out>
    </testcase>
    <testcase classname="live.r2" name="test_zero_byte_round_trip" time="0.10">
      <failure message="request to {_FAILURE_TOKEN} failed">{_TRACEBACK_TOKEN}</failure>
      <system-out>{extra_field_record}</system-out>
    </testcase>
    <testcase classname="live.r2" name="test_zero_byte_round_trip" time="0.10">
      <failure message="request to {_FAILURE_TOKEN} failed">{_TRACEBACK_TOKEN}</failure>
      <system-out>{wrong_stage_record}</system-out>
    </testcase>
    <testcase classname="live.r2" name="test_zero_byte_round_trip" time="0.10">
      <failure message="request to {_FAILURE_TOKEN} failed">{_TRACEBACK_TOKEN}</failure>
      <system-out>{wrong_reason_record}</system-out>
    </testcase>
    <testcase classname="live.r2" name="test_zero_byte_round_trip" time="0.10">
      <failure message="request to {_FAILURE_TOKEN} failed">{_TRACEBACK_TOKEN}</failure>
      <system-out>{_zero_byte_record()}&#10;{_zero_byte_record()}</system-out>
    </testcase>
    <testcase classname="live.r2" name="test_zero_byte_round_trip" time="0.10">
      <failure message="request to {_FAILURE_TOKEN} failed">{_TRACEBACK_TOKEN}</failure>
      <system-out>
        pytest captured stdout header
        {_zero_byte_record()}
        {extra_field_record}
        pytest captured stdout trailer
      </system-out>
    </testcase>
    <testcase classname="live.r2" name="test_multi_chunk_round_trip" time="0.10">
      <failure message="request to {_FAILURE_TOKEN} failed">{_TRACEBACK_TOKEN}</failure>
      <system-out>{_zero_byte_record()}</system-out>
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
        _ORDINARY_OUTPUT_TOKEN,
        _MALFORMED_RECORD_TOKEN,
        _NESTED_FAILURE_TOKEN,
        _STREAM_ATTRIBUTE_TOKEN,
        _STREAM_DESCENDANT_TOKEN,
        _STREAM_TAIL_TOKEN,
    ):
        assert forbidden not in sanitized

    root = ElementTree.parse(sanitized_report).getroot()
    test_cases = root.findall(".//testcase")
    assert [(case.get("name"), case.get("time")) for case in test_cases] == [
        ("test_call_failure", "0.50"),
        ("test_setup_failure", "0.75"),
        ("test_zero_byte_round_trip", "0.10"),
        ("test_zero_byte_round_trip", "0.10"),
        ("test_zero_byte_round_trip", "0.10"),
        ("test_zero_byte_round_trip", "0.10"),
        ("test_zero_byte_round_trip", "0.10"),
        ("test_zero_byte_round_trip", "0.10"),
        ("test_zero_byte_round_trip", "0.10"),
        ("test_multi_chunk_round_trip", "0.10"),
    ]
    failure = root.find(".//failure")
    error = root.find(".//error")
    assert failure is not None and error is not None
    assert failure.get("message") == "r2_live_failure_details_redacted"
    assert error.get("message") == "r2_live_failure_details_redacted"
    assert failure.text == "r2_live_failure_details_redacted"
    assert error.text == "r2_live_failure_details_redacted"
    assert root.find(".//properties") is None
    retained_streams = root.findall(".//system-out")
    assert len(retained_streams) == 1
    assert retained_streams[0].text == _zero_byte_record()
    assert retained_streams[0].attrib == {}
    assert list(retained_streams[0]) == []
    assert not retained_streams[0].tail or not retained_streams[0].tail.strip()
    assert root.find(".//system-err") is None


def test_sanitizer_extracts_diagnostic_from_actual_pytest_junit_envelope(
    tmp_path: Path,
) -> None:
    """The production pytest flags must yield an envelope the sanitizer can consume."""

    raw_report = tmp_path / "object-storage-live-raw.xml"
    sanitized_report = tmp_path / "object-storage-live.xml"
    generated_test = tmp_path / "test_pytest_junit_envelope_fixture.py"
    diagnostic = _zero_byte_record(stage="read", reason="provider_transport_error")
    generated_test.write_text(
        "\n".join(
            (
                "def test_zero_byte_round_trip():",
                f"    print({diagnostic!r})",
                "    assert False",
                "",
                "def test_passing_output_is_not_logged():",
                f"    print({_PASSING_OUTPUT_TOKEN!r})",
            )
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(generated_test),
            "-q",
            "--tb=no",
            "--no-summary",
            f"--junitxml={raw_report}",
            "-o",
            "junit_logging=system-out",
            "-o",
            "junit_log_passing_tests=false",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 1
    raw_root = ElementTree.parse(raw_report).getroot()
    failed_case = next(
        case
        for case in raw_root.findall(".//testcase")
        if case.get("name") == "test_zero_byte_round_trip"
    )
    raw_stream = failed_case.find("system-out")
    assert raw_stream is not None and raw_stream.text is not None
    assert diagnostic in raw_stream.text
    assert raw_stream.text != diagnostic
    passed_case = next(
        case
        for case in raw_root.findall(".//testcase")
        if case.get("name") == "test_passing_output_is_not_logged"
    )
    assert passed_case.find("system-out") is None
    assert _PASSING_OUTPUT_TOKEN not in raw_report.read_text(encoding="utf-8")

    live_conftest.sanitize_live_junit_report(raw_report, sanitized_report)

    sanitized_root = ElementTree.parse(sanitized_report).getroot()
    retained_streams = sanitized_root.findall(".//system-out")
    assert len(retained_streams) == 1
    assert retained_streams[0].text == diagnostic
    failure = sanitized_root.find(".//failure")
    assert failure is not None
    assert failure.text == "r2_live_failure_details_redacted"
