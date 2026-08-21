"""Tests for the canonical, telemetry-safe source locator value."""

from __future__ import annotations

import pytest

from personal_os.source_locators import NormalizedLocator


@pytest.mark.parametrize(
    "value",
    [
        "notes/planning.md",
        "assets/attachments/hoá-đơn.pdf",
        "Journal/2026/Ánh sáng.md",
    ],
)
def test_locator_preserves_canonical_unicode_and_case(value: str) -> None:
    assert NormalizedLocator(value).value == value


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("", "non-empty"),
        ("notes\\planning.md", "backslash"),
        ("/absolute/path.md", "absolute"),
        ("notes//planning.md", "segment"),
        ("notes/./planning.md", "segment"),
        ("notes/../planning.md", "segment"),
        ("C:/notes/planning.md", "scheme or drive"),
        ("https:/notes/planning.md", "scheme or drive"),
        ("notes/plan\u0000.md", "control"),
        ("notes/cafe\u0301.md", "NFC"),
        ("/".join(["deep"] * 257) + ".md", "segments"),
        ("x/" * 255 + "y" * 4000, "UTF-8 bytes"),
    ],
)
def test_locator_rejects_noncanonical_values(value: str, pattern: str) -> None:
    with pytest.raises(ValueError, match=pattern):
        NormalizedLocator(value)


def test_locator_repr_redacts_the_canonical_value() -> None:
    locator = NormalizedLocator("private/notes/secret.md")

    assert repr(locator) == "NormalizedLocator(value=<redacted>)"
    assert "secret" not in repr(locator)
