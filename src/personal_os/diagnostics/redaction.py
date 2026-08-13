"""Bounded payload inspection and deterministic fingerprints for diagnostics.

This module owns the closed redaction boundary used by ``build_registered_event``:
it walks untrusted diagnostic values with fixed depth and item budgets, never
copies an offending key, value, type representation or exception message into a
return value or thrown exception, and reduces dependency text and exception
metadata to short hexadecimal fingerprints.
"""

from __future__ import annotations

import hashlib
import re
import traceback
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from personal_os.diagnostics.events import SafeToken, ShortDigest

MAX_DIAGNOSTIC_DEPTH: Final[int] = 8
MAX_DIAGNOSTIC_ITEMS: Final[int] = 64
MAX_SAFE_INTEGER: Final[int] = 2**63 - 1

FORBIDDEN_NORMALIZED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "content",
        "body",
        "query",
        "excerpt",
        "citationtext",
        "prompt",
        "completion",
        "token",
        "secret",
        "password",
        "credential",
        "authorization",
        "cookie",
        "signedurl",
        "path",
        "vector",
        "embedding",
        "traceback",
        "exceptionmessage",
    }
)

# Defense-in-depth shapes scanned on every raw string value. Raw strings are
# already outside the safe scalar union, so a match cannot change the verdict;
# it only keeps the redaction boundary explicit if the union ever widens.
_SENSITIVE_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"bearer"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|-{5}BEGIN[A-Z ]*PRIVATE[A-Z ]*KEY-{5}"
    r"|[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@"
    r"|x-amz-credential"
    r"|x-amz-signature"
    r"|x-goog-credential"
    r"|sig="
    r"|token=",
    re.IGNORECASE,
)


@dataclass
class _TraversalState:
    """Mutable item counter shared across one value traversal."""

    items_seen: int = 0


def _normalize_key(key: str) -> str:
    return "".join(character for character in key.lower() if character.isalnum())


def _is_safe_scalar(value: object) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return 0 <= value <= MAX_SAFE_INTEGER
    if isinstance(value, StrEnum):
        return True
    if isinstance(value, UUID):
        return True
    return isinstance(value, (SafeToken, ShortDigest))


def _is_sensitive_string(value: str) -> bool:
    return _SENSITIVE_VALUE_PATTERN.search(value) is not None


def _inspect(value: object, depth: int, state: _TraversalState) -> bool:
    if depth > MAX_DIAGNOSTIC_DEPTH:
        return False
    if _is_safe_scalar(value):
        return True
    if isinstance(value, str):
        # Raw strings are outside the safe scalar union and are always rejected.
        # The sensitive-shape scan runs as defense in depth per the boundary
        # contract; its result cannot admit an otherwise-unsafe value.
        _ = _is_sensitive_string(value)
        return False
    if isinstance(value, tuple):
        return _inspect_tuple(value, depth, state)
    if isinstance(value, Mapping):
        _inspect_mapping(value, depth, state)
        return False
    if isinstance(value, (list, set, frozenset)):
        _inspect_sequence(value, depth, state)
        return False
    return False


def _inspect_tuple(value: tuple[object, ...], depth: int, state: _TraversalState) -> bool:
    has_only_scalars = True
    for item in value:
        state.items_seen += 1
        if state.items_seen > MAX_DIAGNOSTIC_ITEMS:
            return False
        if _is_safe_scalar(item):
            continue
        has_only_scalars = False
        _inspect(item, depth + 1, state)
    return has_only_scalars


def _inspect_mapping(value: Mapping[object, object], depth: int, state: _TraversalState) -> None:
    for key, nested in value.items():
        state.items_seen += 1
        if state.items_seen > MAX_DIAGNOSTIC_ITEMS:
            return
        if not isinstance(key, str):
            continue
        if _normalize_key(key) in FORBIDDEN_NORMALIZED_KEYS:
            continue
        _inspect(nested, depth + 1, state)


def _inspect_sequence(value: Iterable[object], depth: int, state: _TraversalState) -> None:
    for item in value:
        state.items_seen += 1
        if state.items_seen > MAX_DIAGNOSTIC_ITEMS:
            return
        _inspect(item, depth + 1, state)


def _is_safe_diagnostic_value(value: object) -> bool:
    state = _TraversalState()
    try:
        return _inspect(value, 0, state)
    except Exception:
        # Hostile objects may raise from arbitrary protocol hooks; they are
        # never safe and must never propagate out of the diagnostic boundary.
        return False


def fingerprint_text(value: str) -> ShortDigest:
    """Return the first 16 lowercase hex characters of SHA-256 over UTF-8 text."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return ShortDigest(digest)


def fingerprint_stack(exception: BaseException) -> ShortDigest:
    """Fingerprint an exception's stack from function name and line number only.

    Filenames, source lines, local values and exception arguments are excluded.
    An exception without a traceback hashes the constant ``"no_stack"``.
    """
    extracted = traceback.extract_tb(exception.__traceback__)
    if not extracted:
        material = "no_stack"
    else:
        material = "\n".join(f"{frame.name}:{frame.lineno}" for frame in extracted)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return ShortDigest(digest)


def normalize_exception_type(exception: BaseException) -> SafeToken:
    """Reduce an exception's type to a bounded ASCII token without stringifying it.

    Uses only ``type(exception).__module__`` and ``type(exception).__qualname__``,
    lowercases ASCII alphanumerics, collapses every other run into ``.`` and
    returns at most 64 characters. Empty, too long or non-conforming names fall
    back to ``exception.<16-hex type-name digest>``.
    """
    qualified = f"{type(exception).__module__}.{type(exception).__qualname__}"
    normalized: list[str] = []
    is_previous_separator = False
    for character in qualified:
        if character.isascii() and character.isalnum():
            normalized.append(character.lower())
            is_previous_separator = False
        elif not is_previous_separator:
            normalized.append(".")
            is_previous_separator = True
    token = "".join(normalized).strip(".")
    if token and len(token) <= 64:
        try:
            return SafeToken.parse(token)
        except ValueError:
            pass
    digest = fingerprint_text(qualified)
    return SafeToken.parse(f"exception.{digest.value}")
