"""Tests for versioned, locator-derived source titles."""

from __future__ import annotations

import pytest

from personal_os.source_lifecycle.title import derive_title_v1
from personal_os.source_locators import NormalizedLocator


def test_title_removes_only_the_final_extension() -> None:
    title = derive_title_v1(NormalizedLocator("notes/release.v1.final.md"))
    assert title.value == "release.v1.final"


def test_title_preserves_normalized_unicode() -> None:
    assert derive_title_v1(NormalizedLocator("notes/hoá-đơn.pdf")).value == "hoá-đơn"


@pytest.mark.parametrize("locator", ["notes/.md"], ids=["empty"])
def test_title_rejects_an_empty_final_filename(locator: str) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        derive_title_v1(NormalizedLocator(locator))


def test_title_rejects_a_result_over_500_characters() -> None:
    with pytest.raises(ValueError, match="500"):
        derive_title_v1(NormalizedLocator(f"notes/{'x' * 501}.md"))
