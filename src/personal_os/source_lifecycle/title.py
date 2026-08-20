"""Versioned source title derivation for lifecycle rename transitions."""

from __future__ import annotations

from personal_os.source_locators import NormalizedLocator
from personal_os.sources.commands import SourceTitle


def derive_title_v1(target_locator: NormalizedLocator) -> SourceTitle:
    """Derive a title from the final locator segment, removing one suffix only."""

    filename = target_locator.value.rsplit("/", 1)[-1]
    title = filename.rsplit(".", 1)[0] if "." in filename else filename
    return SourceTitle(title)
