"""Shared fixtures for recovery unit tests.

On Windows the default pytest temporary tree is deep enough that canonical
object paths (staging prefix + UUIDv7 + unguessable nonce + ``objects/sha256``
sharding + the 64-character digest) exceed ``MAX_PATH`` while long-path
support is disabled; bundle roots therefore use a short temporary directory
there. On every other platform the fixture is the ordinary ``tmp_path``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def bundle_root(tmp_path: Path) -> Iterator[Path]:
    if os.name != "nt":
        yield tmp_path
        return
    root = Path(tempfile.mkdtemp(prefix="recovery-bundle-"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
