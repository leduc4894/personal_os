"""Closed RFC 8785-compatible canonical JSON encoder for signed policy bytes.

The encoder accepts only the value shapes reachable from the fixed snapshot
and keyset schemas (spec 12 and 13): ``null``, booleans, integers inside the
IEEE 754 double-safe range, arrays as tuples, objects as mappings with string
member names, and strings that are valid normalized Unicode. Floats, lists,
bytes, arbitrary objects, non-string member names, duplicate members, lone or
paired surrogates, non-NFC strings and out-of-range integers are rejected
with the typed :class:`PolicyContractError` before any byte is produced, so
nothing outside the closed grammar can ever be signed or hashed.

Serialization follows RFC 8785: members sort by the UTF-16 code units of their
names, no insignificant whitespace is emitted, strings escape only ``"``,
``\\`` and the C0 controls (the two-character short escapes where defined,
lowercase ``\\u00xx`` otherwise) and emit every other code point as raw UTF-8,
and integers render as plain decimal without leading zeros or a sign on zero.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Final

from personal_os.exclusion_policy.errors import (
    PAYLOAD_INTEGER_OUT_OF_RANGE,
    PAYLOAD_MEMBER_DUPLICATE,
    PAYLOAD_MEMBER_NAME_INVALID,
    PAYLOAD_STRING_INVALID_UNICODE,
    PAYLOAD_STRING_NOT_NORMALIZED,
    PAYLOAD_VALUE_UNSUPPORTED,
    payload_contract_error,
)

type CanonicalJsonValue = (
    None | bool | int | str | tuple["CanonicalJsonValue", ...] | Mapping[str, "CanonicalJsonValue"]
)

#: Largest integer exactly representable as an IEEE 754 double (2**53 - 1).
#: RFC 8785 numbers must round-trip through ECMAScript, so anything outside
#: ``[-9007199254740991, 9007199254740991]`` is rejected, never truncated.
MAXIMUM_SAFE_INTEGER: Final[int] = 9007199254740991

#: Short two-character escapes for the C0 controls where JSON defines them.
_CONTROL_ESCAPES: Final[Mapping[int, str]] = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
}

_UNICODE_REPLACEMENT_LOW: Final[int] = 0xD800
_UNICODE_REPLACEMENT_HIGH: Final[int] = 0xDFFF


def _has_surrogate_code_point(value: str) -> bool:
    """Report whether the string contains any UTF-16 surrogate code point."""

    return any(
        _UNICODE_REPLACEMENT_LOW <= ord(character) <= _UNICODE_REPLACEMENT_HIGH
        for character in value
    )


def _validate_string(value: str) -> None:
    """Reject surrogate-bearing and non-NFC strings before serialization."""

    if _has_surrogate_code_point(value):
        raise payload_contract_error(PAYLOAD_STRING_INVALID_UNICODE)
    if not unicodedata.is_normalized("NFC", value):
        raise payload_contract_error(PAYLOAD_STRING_NOT_NORMALIZED)


def _utf16_sort_key(name: str) -> bytes:
    """Sort key placing names in RFC 8785 UTF-16 code-unit order.

    The big-endian UTF-16 encoding of a surrogate-free string is exactly its
    code-unit sequence, so lexicographic byte comparison equals code-unit
    comparison — the order ECMAScript ``Array.prototype.sort`` produces.
    """

    return name.encode("utf-16-be")


def _encode_string(value: str) -> str:
    """Escape the closed minimal set; every other code point stays literal."""

    pieces: list[str] = ['"']
    for character in value:
        if character == '"' or character == "\\":
            pieces.append("\\" + character)
            continue
        code_point = ord(character)
        if code_point < 0x20:
            pieces.append(_CONTROL_ESCAPES.get(code_point, f"\\u{code_point:04x}"))
            continue
        pieces.append(character)
    pieces.append('"')
    return "".join(pieces)


def _encode_into(value: object, pieces: list[bytes]) -> None:
    """Append the canonical UTF-8 bytes of one closed value onto ``pieces``."""

    if value is None:
        pieces.append(b"null")
        return
    if value is True:
        pieces.append(b"true")
        return
    if value is False:
        pieces.append(b"false")
        return
    if isinstance(value, int):
        if not -MAXIMUM_SAFE_INTEGER <= value <= MAXIMUM_SAFE_INTEGER:
            raise payload_contract_error(PAYLOAD_INTEGER_OUT_OF_RANGE)
        pieces.append(str(value).encode("ascii"))
        return
    if isinstance(value, str):
        _validate_string(value)
        pieces.append(_encode_string(value).encode("utf-8"))
        return
    if isinstance(value, tuple):
        pieces.append(b"[")
        for index, element in enumerate(value):
            if index:
                pieces.append(b",")
            _encode_into(element, pieces)
        pieces.append(b"]")
        return
    if isinstance(value, Mapping):
        members = list(value.items())
        seen_names: set[str] = set()
        for name, _ in members:
            if not isinstance(name, str):
                raise payload_contract_error(PAYLOAD_MEMBER_NAME_INVALID)
            _validate_string(name)
            if name in seen_names:
                raise payload_contract_error(PAYLOAD_MEMBER_DUPLICATE)
            seen_names.add(name)
        ordered_members = sorted(members, key=lambda member: _utf16_sort_key(member[0]))
        pieces.append(b"{")
        for index, (name, member_value) in enumerate(ordered_members):
            if index:
                pieces.append(b",")
            pieces.append(_encode_string(name).encode("utf-8"))
            pieces.append(b":")
            _encode_into(member_value, pieces)
        pieces.append(b"}")
        return
    raise payload_contract_error(PAYLOAD_VALUE_UNSUPPORTED)


def canonicalize_json_value(value: object) -> bytes:
    """Serialize one closed value to exact RFC 8785 canonical UTF-8 bytes.

    The input is the typed closed union built by the signed payload builders
    (or an equally closed caller-owned value); anything outside the grammar
    raises :class:`PolicyContractError` carrying a single closed reason token,
    never the rejected value itself.
    """

    pieces: list[bytes] = []
    _encode_into(value, pieces)
    return b"".join(pieces)
