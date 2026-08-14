"""Canonical SHA-256 digest, object key and media-type value objects.

These are the transport-neutral identity values for content-addressed objects.
SHA-256 is the sole content identity; the digest is exactly 64 lowercase
hexadecimal characters and the only canonical key grammar is
``objects/sha256/{first_2}/{next_2}/{sha256}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

_DIGEST_HEX_LENGTH: Final[int] = 64
_HEX_LOWER: Final[frozenset[str]] = frozenset("0123456789abcdef")
_KEY_PREFIX: Final[str] = "objects/sha256"
# RFC 2045 ``tspecials`` plus the ``/`` separator; these cannot appear inside a
# canonical media token.
_TSPECIALS: Final[frozenset[str]] = frozenset(
    {"(", ")", "<", ">", "@", ",", ";", ":", "\\", '"', "/", "[", "]", "?", "="}
)


@dataclass(frozen=True, slots=True)
class ContentDigest:
    """Lowercase hexadecimal SHA-256 digest of canonical object bytes."""

    hexadecimal: str

    @classmethod
    def parse(cls, value: str) -> ContentDigest:
        """Validate ``value`` as exactly 64 lowercase hexadecimal characters.

        Uppercase hex, prefixes such as ``sha256:``, surrounding whitespace,
        wrong length and non-hexadecimal values are rejected.
        """
        if len(value) != _DIGEST_HEX_LENGTH or any(char not in _HEX_LOWER for char in value):
            raise ValueError("value does not satisfy the canonical digest contract")
        return cls(value)

    def __str__(self) -> str:
        return self.hexadecimal


@dataclass(frozen=True, slots=True)
class CanonicalObjectKey:
    """Content-addressable key under the only canonical SHA-256 grammar."""

    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class CanonicalMediaType:
    """Lowercase canonical MIME ``type/subtype`` with no parameters."""

    value: str

    @classmethod
    def parse(cls, value: str) -> CanonicalMediaType:
        """Validate ``value`` as a lowercase ``type/subtype`` MIME token pair.

        Parameters such as ``; charset=utf-8``, whitespace, control characters,
        uppercase letters, wildcard values and an empty ``type`` or ``subtype``
        are rejected.
        """
        type_part, separator, subtype_part = value.partition("/")
        if separator != "/" or not type_part or not subtype_part or "/" in subtype_part:
            raise ValueError("value does not satisfy the canonical media type contract")
        for char in value:
            if char == "/" or _is_canonical_token_char(char):
                continue
            raise ValueError("value does not satisfy the canonical media type contract")
        return cls(value)

    def __str__(self) -> str:
        return self.value


def _is_canonical_token_char(char: str) -> bool:
    code_point = ord(char)
    if code_point < 33 or code_point > 126:
        return False
    if char in _TSPECIALS:
        return False
    if "A" <= char <= "Z":
        return False
    return char != "*"


def derive_canonical_object_key(digest: ContentDigest) -> CanonicalObjectKey:
    """Derive the only valid canonical key for ``digest``.

    The key is ``objects/sha256/{digest[0:2]}/{digest[2:4]}/{digest}`` and has no
    workspace, source, filename, date, environment or media-type component.
    """
    hexadecimal = digest.hexadecimal
    return CanonicalObjectKey(f"{_KEY_PREFIX}/{hexadecimal[0:2]}/{hexadecimal[2:4]}/{hexadecimal}")
