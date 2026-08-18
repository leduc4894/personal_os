"""Shared fixtures for the reference performance gates.

The performance harnesses drive psycopg async connections through real
disposable-stack engines; on Windows the default proactor loop cannot carry
them, so every asyncio test and fixture in this directory runs on a selector
event loop (already the CI default on Linux).
"""

from __future__ import annotations

import asyncio
from asyncio import AbstractEventLoop
from collections.abc import Callable

import pytest


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, Callable[[], AbstractEventLoop]]:
    del config, item
    return {"selector": asyncio.SelectorEventLoop}
