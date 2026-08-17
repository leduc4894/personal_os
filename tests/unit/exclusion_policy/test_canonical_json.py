"""Closed RFC 8785-compatible canonical JSON encoder for signed policy payloads.

The encoder is closed by design: only the value shapes reachable from the
fixed snapshot/keyset schemas are accepted (ASCII member names, integers,
booleans, null, arrays, normalized valid Unicode strings). Floats,
non-normalized strings, lone surrogates, duplicate members, non-string member
names, out-of-range integers and unknown value types are rejected before any
bytes are signed. The RFC 8785 section 3.2.3 sorting example is replayed as
an authoritative cross-language ordering and escaping vector, expressed over
the NFC form of its inputs: RFC 8785 preserves strings "as is", while this
closed encoder admits normalized Unicode only, so the example's one non-NFC
key (U+FB33, a Hebrew presentation form) enters as its NFC decomposition.
"""

from __future__ import annotations

import pytest

from personal_os.exclusion_policy.canonical_json import canonicalize_json_value
from personal_os.exclusion_policy.errors import PolicyContractError

#: Exact canonical text of the RFC 8785 section 3.2.3 sorting example after
#: NFC preprocessing. Members sort by UTF-16 code units, so the emoji (first
#: surrogate 0xD83D) sorts last even though its code point is the largest,
#: and the decomposed Hebrew pair U+05D3 U+05BC sorts between U+00F6 and
#: U+20AC; U+0080 is emitted as a literal character, not an escape.
RFC_8785_SORTING_EXAMPLE_INPUT = {
    "\u20ac": "Euro Sign",
    "\r": "Carriage Return",
    "\u05d3\u05bc": "Hebrew Letter Dalet With Dagesh",
    "1": "One",
    "\U0001f600": "Emoji: Grinning Face",
    "\u0080": "Control",
    "\u00f6": "Latin Small Letter O With Diaeresis",
}

RFC_8785_SORTING_EXAMPLE_OUTPUT = (
    '{"\\r":"Carriage Return","1":"One","\u0080":"Control",'
    '"ö":"Latin Small Letter O With Diaeresis",'
    '"\u05d3\u05bc":"Hebrew Letter Dalet With Dagesh",'
    '"€":"Euro Sign","\U0001f600":"Emoji: Grinning Face"}'
)


def test_rfc_8785_sorting_example_produces_exact_canonical_bytes() -> None:
    encoded = canonicalize_json_value(RFC_8785_SORTING_EXAMPLE_INPUT)
    assert encoded == RFC_8785_SORTING_EXAMPLE_OUTPUT.encode("utf-8")


def test_rfc_8785_example_non_nfc_hebrew_key_is_rejected() -> None:
    # RFC 8785 preserves U+FB33 "as is"; this closed encoder admits
    # normalized Unicode only, so the Hebrew presentation form must be
    # rejected rather than silently signed.
    with pytest.raises(PolicyContractError):
        canonicalize_json_value({"\ufb33": "Hebrew Letter Dalet With Dagesh"})


def test_utf16_member_order_moves_astral_characters_before_high_bmp_names() -> None:
    # Code-point order would place U+FF01 before U+1F600; UTF-16 code-unit
    # order (0xD83D... < 0xFF01) is what RFC 8785 mandates.
    encoded = canonicalize_json_value({"\uff01": 1, "\U0001f600": 2})
    assert encoded == '{"\U0001f600":2,"\uff01":1}'.encode("utf-8")


def test_scalar_values_encode_without_whitespace() -> None:
    assert canonicalize_json_value(None) == b"null"
    assert canonicalize_json_value(True) == b"true"
    assert canonicalize_json_value(False) == b"false"
    assert canonicalize_json_value(0) == b"0"
    assert canonicalize_json_value(-12) == b"-12"
    assert canonicalize_json_value(9007199254740991) == b"9007199254740991"
    assert canonicalize_json_value(-9007199254740991) == b"-9007199254740991"


def test_containers_encode_members_and_elements_without_whitespace() -> None:
    assert canonicalize_json_value({}) == b"{}"
    assert canonicalize_json_value(()) == b"[]"
    nested = {"b": (1, None, False), "a": {}}
    assert canonicalize_json_value(nested) == b'{"a":{},"b":[1,null,false]}'


def test_strings_emit_raw_utf8_and_escape_only_the_closed_escape_set() -> None:
    value = {
        "quote": 'a"b',
        "backslash": "a\\b",
        "controls": "\b\t\n\f\r",
        "unit_separator": "a\x1fb",
        "nfc_text": "öác",
    }
    encoded = canonicalize_json_value(value)
    expected = (
        '{"backslash":"a\\\\b","controls":"\\b\\t\\n\\f\\r",'
        '"nfc_text":"öác","quote":"a\\"b","unit_separator":"a\\u001fb"}'
    )
    assert encoded == expected.encode()


@pytest.mark.parametrize(
    "value",
    [1.5, float("nan"), float("inf"), float("-inf"), float("-0.0")],
)
def test_closed_canonicalizer_rejects_floats(value: object) -> None:
    with pytest.raises(PolicyContractError):
        canonicalize_json_value(value)


def test_closed_canonicalizer_rejects_non_nfc_values() -> None:
    with pytest.raises(PolicyContractError):
        canonicalize_json_value("e\u0301")


@pytest.mark.parametrize(
    "value",
    [
        "lone surrogate: \ud800",
        "paired surrogates: \ud83d\ude00",
        {"key\udfff": "value"},
        {"nested": ("ok", "\ud800")},
    ],
)
def test_closed_canonicalizer_rejects_surrogates(value: object) -> None:
    with pytest.raises(PolicyContractError):
        canonicalize_json_value(value)


@pytest.mark.parametrize(
    "value",
    [
        9007199254740992,
        -9007199254740992,
        [1, 2],
        {"a": 1.0},
        b"bytes",
        {"a": b"bytes"},
        object(),
        {"a": object()},
    ],
)
def test_closed_canonicalizer_rejects_unknown_value_shapes(value: object) -> None:
    with pytest.raises(PolicyContractError):
        canonicalize_json_value(value)


@pytest.mark.parametrize("value", [{1: "one"}, {None: "null"}, {b"a": "bytes"}])
def test_closed_canonicalizer_rejects_non_string_member_names(value: object) -> None:
    with pytest.raises(PolicyContractError):
        canonicalize_json_value(value)


def test_closed_canonicalizer_rejects_duplicate_members() -> None:
    class DuplicateMemberMapping(dict[str, object]):
        """Mapping whose items() yield one member name twice."""

        def items(self) -> tuple[tuple[str, object], ...]:  # type: ignore[override]
            return (("a", 1), ("a", 2))

    with pytest.raises(PolicyContractError):
        canonicalize_json_value(DuplicateMemberMapping())


def test_rejections_carry_safe_closed_reason_tokens_only() -> None:
    for hostile_value in (1.5, "e\u0301", [1], {1: 2}, float("nan")):
        with pytest.raises(PolicyContractError) as raised:
            canonicalize_json_value(hostile_value)
        assert set(raised.value.safe_details) == {"reason"}
