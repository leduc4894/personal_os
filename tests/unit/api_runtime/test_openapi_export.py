"""Deterministic OpenAPI export: normalization, byte stability and offline write.

These tests pin the exporter contract: the rendered document is byte-identical
across renders and ends with one newline, the closed health route set carries
the canonical operation ids, mapping keys are sorted recursively while arrays
keep their order, the document-level ``servers`` binding is removed, and
unsupported non-JSON values are rejected instead of coerced. One export must
also complete without reading the process environment, opening a socket or
touching a database engine, and it must never create parent directories or
leave a partial file behind when rendering fails.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import NoReturn

import pytest
from api_runtime import openapi_export
from api_runtime.openapi_export import export_openapi, normalize_openapi, render_openapi_json
from sqlalchemy.ext.asyncio import AsyncEngine

#: Pydantic's plugin loader reads this one non-secret discovery flag through
#: ``os.environ.get`` on every model and schema build inside FastAPI
#: application composition; there is no code path that composes the export
#: application without triggering it. Answering ``"__all__"`` disables plugin
#: discovery outright, which also keeps rendering independent of whatever
#: pydantic plugins happen to be installed.
_FRAMEWORK_PLUGIN_FLAG = "PYDANTIC_DISABLE_PLUGINS"

#: Pytest unconditionally writes and pops its own phase-bookkeeping variable
#: (``PYTEST_CURRENT_TEST``, the node id plus phase) around every test phase,
#: including the patched test's teardown before the patch is undone.
_RUNNER_BOOKKEEPING_KEY = "PYTEST_CURRENT_TEST"

#: Operational keys of the frameworks hosting the run — pytest's terminal
#: rendering (``PY_COLORS``, ``NO_COLOR``, ``FORCE_COLOR``, ``TERM``, terminal
#: size via ``COLUMNS``/``LINES``) — are answered as "unset" with ``KeyError``,
#: the protocol-correct absent answer those consumers already degrade from.
#: They carry no application configuration or secret. Every other key access
#: raises ``AssertionError`` and fails the test.
_ABSENT_FRAMEWORK_KEYS = frozenset(
    {"PY_COLORS", "NO_COLOR", "FORCE_COLOR", "TERM", "COLUMNS", "LINES"}
)


class ForbiddenEnvironment(MutableMapping[str, str]):
    """Environment replacement that fails any test reading or scanning it.

    The ``Mapping`` mixins funnel ``get``, membership and view methods through
    ``__getitem__``/``__iter__``/``__len__``, and those raise ``AssertionError``
    rather than ``KeyError`` so the mixins cannot swallow the failure. The only
    tolerated traffic is the hosting frameworks' own operational keys
    documented above: pydantic's plugin flag, pytest's phase bookkeeping and
    pytest's terminal-rendering lookups. No application setting, secret or
    machine value is ever readable.
    """

    def __init__(self) -> None:
        self._bookkeeping: dict[str, str] = {}

    def __getitem__(self, key: str) -> str:
        if key == _FRAMEWORK_PLUGIN_FLAG:
            return "__all__"
        if key == _RUNNER_BOOKKEEPING_KEY:
            return self._bookkeeping[key]
        if key in _ABSENT_FRAMEWORK_KEYS:
            raise KeyError(key)
        raise AssertionError(f"environment variable read during export: {key}")

    def __setitem__(self, key: str, value: str) -> None:
        if key == _RUNNER_BOOKKEEPING_KEY:
            self._bookkeeping[key] = value
            return
        raise AssertionError(f"environment variable written during export: {key}")

    def __delitem__(self, key: str) -> None:
        if key == _RUNNER_BOOKKEEPING_KEY:
            del self._bookkeeping[key]
            return
        raise AssertionError(f"environment variable deleted during export: {key}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("environment scanned during export")

    def __len__(self) -> int:
        raise AssertionError("environment size read during export")


def forbid(*args: object, **kwargs: object) -> NoReturn:
    """Fail any test whose patched network or database entry point is called."""
    del args, kwargs
    raise AssertionError("network or database access attempted during export")


def test_openapi_render_is_byte_identical_and_has_no_machine_values() -> None:
    first = render_openapi_json()
    second = render_openapi_json()
    assert first == second
    assert first.endswith(b"\n")
    document = json.loads(first)
    assert document["openapi"] == "3.1.0"
    assert "servers" not in document
    assert set(document["paths"]) == {
        "/api/health/live",
        "/api/health/ready",
        "/api/auth/login",
        "/api/auth/session",
        "/api/auth/logout",
        "/api/auth/reauthenticate",
        "/api/auth/password",
        "/api/auth/totp/verify",
        "/api/auth/totp/enrollments",
        "/api/auth/totp/enrollments/{enrollment_id}/verify",
        "/api/auth/totp/recovery",
        "/api/auth/totp/recovery-codes/regenerate",
        "/api/auth/totp",
        "/api/auth/device-authorizations",
        "/api/auth/device-authorizations/lookup",
        "/api/auth/device-authorizations/{grant_id}/approve",
        "/api/auth/device-authorizations/{grant_id}/deny",
        "/api/auth/device-authorizations/{grant_id}/poll",
    }
    assert document["paths"]["/api/health/live"]["get"]["operationId"] == "getApiLiveness"
    assert document["paths"]["/api/health/ready"]["get"]["operationId"] == "getApiReadiness"
    assert document["paths"]["/api/auth/login"]["post"]["operationId"] == "login"
    assert document["paths"]["/api/auth/session"]["get"]["operationId"] == "getSession"
    assert document["paths"]["/api/auth/logout"]["post"]["operationId"] == "logout"
    assert document["paths"]["/api/auth/reauthenticate"]["post"]["operationId"] == "reauthenticate"
    assert document["paths"]["/api/auth/password"]["put"]["operationId"] == "changePassword"
    assert (
        document["paths"]["/api/auth/totp/verify"]["post"]["operationId"] == "verifyTotpChallenge"
    )
    assert (
        document["paths"]["/api/auth/totp/enrollments"]["post"]["operationId"]
        == "createTotpEnrollment"
    )
    assert (
        document["paths"]["/api/auth/totp/enrollments/{enrollment_id}/verify"]["post"][
            "operationId"
        ]
        == "verifyTotpEnrollment"
    )
    assert (
        document["paths"]["/api/auth/totp/recovery"]["post"]["operationId"] == "startTotpRecovery"
    )
    assert (
        document["paths"]["/api/auth/totp/recovery-codes/regenerate"]["post"]["operationId"]
        == "regenerateTotpRecoveryCodes"
    )
    assert document["paths"]["/api/auth/totp"]["delete"]["operationId"] == "disableTotp"
    assert (
        document["paths"]["/api/auth/device-authorizations"]["post"]["operationId"]
        == "createDeviceAuthorization"
    )
    assert (
        document["paths"]["/api/auth/device-authorizations/lookup"]["post"]["operationId"]
        == "lookupDeviceAuthorization"
    )
    assert (
        document["paths"]["/api/auth/device-authorizations/{grant_id}/approve"]["post"][
            "operationId"
        ]
        == "approveDeviceAuthorization"
    )
    assert (
        document["paths"]["/api/auth/device-authorizations/{grant_id}/deny"]["post"]["operationId"]
        == "denyDeviceAuthorization"
    )
    assert (
        document["paths"]["/api/auth/device-authorizations/{grant_id}/poll"]["post"]["operationId"]
        == "pollDeviceAuthorization"
    )


def test_openapi_render_omits_the_framework_validation_error_documentation() -> None:
    # The runtime answers request-validation failures with the canonical error
    # envelope, so the document must not advertise FastAPI's default 422
    # HTTPValidationError shape on any body-bearing route.
    document = json.loads(render_openapi_json())
    assert "HTTPValidationError" not in document["components"]["schemas"]
    assert "ValidationError" not in document["components"]["schemas"]
    for operations in document["paths"].values():
        for operation in operations.values():
            assert "422" not in operation["responses"]


def test_export_never_reads_environment_secret_or_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(os, "environ", ForbiddenEnvironment())
    monkeypatch.setattr(socket, "create_connection", forbid)
    monkeypatch.setattr(AsyncEngine, "connect", forbid)
    output = tmp_path / "openapi.json"
    assert export_openapi(str(output)) == 0
    assert output.read_bytes() == render_openapi_json()


def test_normalize_openapi_sorts_mapping_keys_recursively_without_touching_arrays() -> None:
    document = {
        "servers": [{"url": "http://replace-me"}],
        "zebra": {"beta": 1, "alpha": 2},
        "alpha": [{"zulu": 1, "alfa": 2}, 3, 3, 1, 2],
    }
    normalized = normalize_openapi(document)
    assert list(normalized) == ["alpha", "zebra"]
    assert "servers" not in normalized
    assert list(normalized["zebra"]) == ["alpha", "beta"]
    assert list(normalized["alpha"][0]) == ["alfa", "zulu"]
    assert normalized["alpha"] == [{"alfa": 2, "zulu": 1}, 3, 3, 1, 2]


def test_normalize_openapi_rejects_unsupported_non_json_values() -> None:
    with pytest.raises(TypeError):
        normalize_openapi({"checks": {"postgresql", "schema"}})


def test_render_failure_leaves_no_output_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_render() -> bytes:
        raise RuntimeError("render failed before any write")

    monkeypatch.setattr(openapi_export, "render_openapi_json", fail_render)
    output = tmp_path / "openapi.json"
    with pytest.raises(RuntimeError):
        export_openapi(str(output))
    assert not output.exists()


def test_export_write_failure_exits_seventy_and_never_creates_parent_directories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "missing" / "openapi.json"
    assert export_openapi(str(output)) == 70
    captured = capsys.readouterr()
    assert "openapi_export_failed" in captured.err
    assert str(output) not in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""
    assert not output.parent.exists()
