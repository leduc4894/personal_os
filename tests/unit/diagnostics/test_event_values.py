from __future__ import annotations

import pytest

from personal_os.diagnostics.events import EventName, SafeToken, ShortDigest


@pytest.mark.parametrize("value", ["api", "runtime_configuration", "provider.model-1"])
def test_safe_token_accepts_registered_ascii_shape(value: str) -> None:
    assert str(SafeToken.parse(value)) == value


@pytest.mark.parametrize("value", ["", "UPPER", "has space", "secret/value", "x" * 65])
def test_safe_token_rejects_unbounded_or_unsafe_text(value: str) -> None:
    with pytest.raises(ValueError, match="safe token"):
        SafeToken.parse(value)


def test_short_digest_requires_sixteen_lowercase_hex_characters() -> None:
    assert str(ShortDigest.parse("0123456789abcdef")) == "0123456789abcdef"
    with pytest.raises(ValueError, match="digest"):
        ShortDigest.parse("0123456789ABCDEf")


def test_event_names_are_closed() -> None:
    assert {event.value for event in EventName} == {
        "runtime_configuration_validated",
        "runtime_configuration_failed",
        "client_request_id_rejected",
        "trace_context_replaced",
        "logging_payload_rejected",
        "dependency_log",
        "internal_error",
    }
