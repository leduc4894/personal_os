"""Deterministic OpenAPI export: normalize, render and write the contract document.

The exporter is offline by construction: it composes the application with the
fixed test environment and a readiness probe that performs no I/O, never enters
the application lifespan, and derives the document purely from the route
definitions. Normalization drops the document-level ``servers`` binding and
sorts mapping keys recursively so one commit always renders byte-identical
bytes, while arrays keep their order because OpenAPI arrays (``required``,
``anyOf``, ``enum``) carry meaning. Unsupported non-JSON values are rejected
instead of silently coerced, the payload reaches disk through a single write
only after the full bytes are rendered, parent directories are never
created implicitly, and a failed write surfaces as exit code ``70`` with one
fixed safe stderr line instead of a traceback.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

from fastapi import FastAPI

from api_runtime.application import create_api_application
from api_runtime.authentication_composition import compose_offline_web_authentication
from api_runtime.exclusion_policy_composition import compose_offline_exclusion_policy
from api_runtime.small_file_sync_composition import compose_offline_small_file_sync
from personal_os.runtime_configuration.models import RuntimeEnvironment

_EXIT_INTERNAL_FAILURE: Final[int] = 70

#: Document-level bindings that embed deployment machine values. Only the root
#: ``servers`` entry is dropped: nested keys named ``servers`` can be ordinary
#: schema properties, and OpenAPI host bindings on this closed route set only
#: ever appear at the document root.
_REMOVED_DOCUMENT_KEYS: Final[frozenset[str]] = frozenset({"servers"})


class _ReadyProbe:
    """Readiness probe stub: the exported document never consults dependencies."""

    async def check(self) -> None: ...


def create_contract_application() -> FastAPI:
    """Compose the offline application whose sole purpose is contract export.

    The fixed test environment keeps the OpenAPI document route enabled, the
    injected probe performs no I/O, and the deterministic offline
    authentication, exclusion-policy and small-file-sync runtimes carry fixed
    non-secret ports: no environment value, key file, database or provider
    client is ever read, and the application lifespan is never entered
    because the document is read directly from the route graph.
    """
    return create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(),
        exclusion_policy=compose_offline_exclusion_policy(),
        small_file_sync=compose_offline_small_file_sync(),
    )


def normalize_openapi(document: Mapping[str, object]) -> dict[str, object]:
    """Return the document with stable key order and machine bindings removed.

    Mapping keys are sorted at every depth and the document-level ``servers``
    entry is dropped; array order is preserved. Any value outside the JSON data
    model raises ``TypeError`` rather than being coerced by a later serializer.
    """
    stripped = {key: document[key] for key in document if key not in _REMOVED_DOCUMENT_KEYS}
    normalized = _normalize_value(stripped)
    if not isinstance(normalized, dict):
        raise TypeError("openapi document root must be a mapping")
    return cast("dict[str, object]", normalized)


def render_openapi_json() -> bytes:
    """Render the deterministic OpenAPI document bytes for the contract snapshot."""
    app = create_contract_application()
    document = normalize_openapi(app.openapi())
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def export_openapi(output_path: str) -> int:
    """Write the rendered document bytes to one file and report the exit code.

    A write failure (for example an output location inside a missing
    directory) is an unexpected internal failure: it returns exit code
    ``70`` with one fixed safe stderr line and never prints the raw
    exception or the failing path.
    """
    payload = render_openapi_json()
    try:
        Path(output_path).write_bytes(payload)
    except OSError:
        print("openapi_export_failed", file=sys.stderr)
        return _EXIT_INTERNAL_FAILURE
    return 0


def _normalize_value(value: object) -> object:
    """Sort mapping keys recursively while preserving array order and scalars."""
    if isinstance(value, dict):
        return {key: _normalize_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, bool | int | float | str | None):
        return value
    raise TypeError(f"unsupported non-json value in openapi document: {type(value).__qualname__}")
