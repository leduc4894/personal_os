"""Private canonical source-locator values.

Raw locators are canonical relational state and are never safe diagnostics or
telemetry values.  Their value object consequently redacts its representation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from unicodedata import category, is_normalized

_LOCATOR_MAXIMUM_BYTES: Final[int] = 4096
_LOCATOR_MAXIMUM_SEGMENTS: Final[int] = 256


@dataclass(frozen=True, slots=True)
class NormalizedLocator:
    """Validated NFC, slash-separated locator relative to one vault."""

    value: str

    def __repr__(self) -> str:
        return "NormalizedLocator(value=<redacted>)"

    def __post_init__(self) -> None:
        if not is_normalized("NFC", self.value):
            raise ValueError("normalized locator must be NFC-normalized")
        if not self.value:
            raise ValueError("normalized locator must be non-empty")
        if "\\" in self.value:
            raise ValueError("normalized locator must not contain a backslash separator")
        if self.value.startswith("/"):
            raise ValueError("normalized locator must not be absolute")
        if self.value.endswith("/"):
            raise ValueError("normalized locator must not have a trailing separator")
        if any(category(char) == "Cc" for char in self.value):
            raise ValueError("normalized locator must not contain control characters")
        segments = self.value.split("/")
        if ":" in segments[0]:
            raise ValueError("normalized locator must not contain a scheme or drive prefix")
        if any(segment in {"", ".", ".."} for segment in segments):
            raise ValueError("normalized locator segments must not be empty, '.' or '..'")
        if len(segments) > _LOCATOR_MAXIMUM_SEGMENTS:
            raise ValueError(
                f"normalized locator must have at most {_LOCATOR_MAXIMUM_SEGMENTS} segments"
            )
        if len(self.value.encode("utf-8")) > _LOCATOR_MAXIMUM_BYTES:
            raise ValueError(
                f"normalized locator must be at most {_LOCATOR_MAXIMUM_BYTES} UTF-8 bytes"
            )
