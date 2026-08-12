"""Installed distribution metadata for composition-root shells."""

from importlib.metadata import version
from typing import Final

DISTRIBUTION_NAME: Final = "knowledge-core"


def distribution_version() -> str:
    return version(DISTRIBUTION_NAME)
