"""Collection rules for the exclusion-policy contract directory.

The reference-device verification gate (``test_reference_device_records``)
validates evidence gathered on physical Desktop and Mobile Obsidian devices,
so it must execute only when explicitly selected — through the dedicated
``poe exclusion-policy-device-verification`` task or an ``-m`` expression
that names ``device_records``. The default marker expression already
deselects it, but the acceptance feature gate's fixed marker override
(``-m "not r2_live"``) would otherwise sweep it in and fail every run while
the physical-device evidence is still being gathered; absence of the records
blocks the final handoff, not every intermediate gate. When the marker is
explicitly requested the test runs and fails — never skips — on missing
evidence.
"""

from __future__ import annotations

import pytest

_EXPLICIT_DEVICE_SELECTION_TOKEN = "device_records"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    marker_expression = str(config.option.markexpr or "")
    if _EXPLICIT_DEVICE_SELECTION_TOKEN in marker_expression:
        return
    deselected = [
        item
        for item in items
        if any(marker.name == _EXPLICIT_DEVICE_SELECTION_TOKEN for marker in item.iter_markers())
    ]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item not in deselected]
