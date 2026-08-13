from __future__ import annotations

import pytest

from personal_os.diagnostics.trace_context import format_traceparent, resolve_trace_context

VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_valid_version_zero_keeps_trace_and_creates_local_span() -> None:
    resolved = resolve_trace_context(VALID)
    assert resolved.was_replaced is False
    assert str(resolved.context.trace_id) == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert str(resolved.context.remote_parent_span_id) == "00f067aa0ba902b7"
    assert len(str(resolved.context.local_span_id)) == 16
    assert format_traceparent(resolved.context).startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")


@pytest.mark.parametrize(
    "value",
    [
        "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
        "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",
        "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "malformed",
    ],
)
def test_invalid_present_header_is_replaced_without_echo(value: str) -> None:
    resolved = resolve_trace_context(value)
    assert resolved.was_replaced is True
    assert value not in repr(resolved)


def test_absent_header_creates_context_without_warning() -> None:
    resolved = resolve_trace_context(None)
    assert resolved.was_replaced is False
