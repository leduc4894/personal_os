"""Read-only R2 HeadBucket runtime-check command contract.

These tests pin the one-shot ``object-storage-check-runtime`` operator command.
``--service`` is parsed into the existing three ``ServiceName`` values before
any environment or secret read; the exact command sequence holds (correlation
context, settings load, safe diagnostics, startup janitor, bounded read-only
``HeadBucket`` probe, exactly one safe JSON event, exactly one client close);
the exit-code matrix ``0``/``2``/``69``/``70``/``78`` is stable; and the
scripted check never invokes put/get/list/delete. Every probe is driven through
the deterministic :class:`ScriptedS3Client`, so no test touches the network.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from botocore.exceptions import ClientError
from tests.contract.object_storage.scripted_s3 import ScriptedS3Client

from personal_os.diagnostics.logging import reset_diagnostics_for_testing
from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.runtime_configuration.models import ServiceName

if TYPE_CHECKING:
    from r2_object_storage.spool import SpoolCleanupSummary

COMMAND_MODULE = "r2_object_storage.runtime_check"
COMMAND_PROGRAM = "object-storage-check-runtime"


def test_operations_guide_documents_closed_cleanup_degradation_reasons() -> None:
    guide = Path(__file__).parents[3] / "docs" / "operations" / "object-storage.md"
    content = guide.read_text(encoding="utf-8")
    for reason in ("spool_cleanup_scan_failed", "object_storage_client_close_degraded"):
        assert reason in content, (
            "the object-storage operations guide must document the closed diagnostic "
            f"reason token {reason!r}"
        )


_VALID_ACCOUNT_ID = "abcdef0123456789abcdef0123456789"
_VALID_ENDPOINT = f"https://{_VALID_ACCOUNT_ID}.r2.cloudflarestorage.com"
_VALID_BUCKET = "knowledge-test"
_ACCESS_KEY_ID_FILE = "r2_access_key_id"
_SECRET_ACCESS_KEY_FILE = "r2_secret_access_key"
_ACCESS_KEY_SECRET_VALUE = "access-key-secret-value"
_SECRET_ACCESS_KEY_VALUE = "secret-access-key-value"

#: The maximum HeadBucket probe attempts the command-level bounded retry uses.
_EXPECTED_MAXIMUM_PROBE_ATTEMPTS = 3

#: Standard-library top-level modules the entry module may import eagerly.
_STDLIB_TOP_LEVEL_PREFIXES = frozenset(
    {
        "__future__",
        "argparse",
        "asyncio",
        "collections",
        "contextlib",
        "dataclasses",
        "pathlib",
        "time",
        "typing",
    }
)


def _client_error(code: str, status: int, operation: str = "HeadBucket") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "scripted"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


def _forbid_side_effect(*_args: object, **_kwargs: object) -> None:
    raise AssertionError(
        "the runtime-check command touched the environment, secret files or the "
        "network before the CLI syntax decision"
    )


def _no_sleep(_delay: float) -> Awaitable[None]:
    async def _sleep() -> None:
        return None

    return _sleep()


class SleepRecorder:
    """Awaitable sleep seam that records every requested delay."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> Awaitable[None]:
        self.delays.append(delay)
        return _no_sleep(delay)


class RecordingClientSource:
    """Injectable client-manager fake counting acquisitions and closes."""

    def __init__(self, client: ScriptedS3Client, order: list[str] | None = None) -> None:
        self._client = client
        self._order = order
        self.get_client_count = 0
        self.close_count = 0

    async def get_client(self) -> ScriptedS3Client:
        self.get_client_count += 1
        if self._order is not None:
            self._order.append("get_client")
        return self._client

    async def close(self) -> None:
        self.close_count += 1


class RaisingClientSource(RecordingClientSource):
    """Client source whose acquisition fails with an unexpected error."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(ScriptedS3Client())
        self._cause = cause

    async def get_client(self) -> ScriptedS3Client:
        await super().get_client()
        raise self._cause


class CloseFailingClientSource(RecordingClientSource):
    """Client source that exposes a close failure without rendering its cause."""

    def __init__(self, client: ScriptedS3Client, cause: BaseException) -> None:
        super().__init__(client)
        self._cause = cause

    async def close(self) -> None:
        await super().close()
        raise self._cause


@pytest.fixture(autouse=True)
def _clean_diagnostics() -> Iterator[None]:
    reset_diagnostics_for_testing()
    yield
    reset_diagnostics_for_testing()


def _secret_files(secret_root: Path) -> None:
    (secret_root / _ACCESS_KEY_ID_FILE).write_text(_ACCESS_KEY_SECRET_VALUE, encoding="utf-8")
    (secret_root / _SECRET_ACCESS_KEY_FILE).write_text(_SECRET_ACCESS_KEY_VALUE, encoding="utf-8")


def _valid_environ(secret_root: Path, spool_root: Path) -> dict[str, str]:
    return {
        "KNOWLEDGE_ENVIRONMENT": "test",
        "KNOWLEDGE_SECRET_ROOT": str(secret_root),
        "KNOWLEDGE_R2_ENDPOINT": _VALID_ENDPOINT,
        "KNOWLEDGE_R2_BUCKET_NAME": _VALID_BUCKET,
        "KNOWLEDGE_R2_ACCESS_KEY_ID_FILE": _ACCESS_KEY_ID_FILE,
        "KNOWLEDGE_R2_SECRET_ACCESS_KEY_FILE": _SECRET_ACCESS_KEY_FILE,
        "KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT": str(spool_root),
    }


def _ordering_janitor(order: list[str]) -> Callable[[Path], Awaitable[SpoolCleanupSummary]]:
    from r2_object_storage.spool import SpoolCleanupSummary as _Summary

    async def _janitor(_spool_root: Path) -> SpoolCleanupSummary:
        order.append("janitor")
        return _Summary(0, 0, 0, 0)

    return _janitor


async def _run_check(
    environ: dict[str, str],
    source: RecordingClientSource,
    *,
    order: list[str] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    spool_janitor: Callable[[Path], Awaitable[SpoolCleanupSummary]] | None = None,
) -> int:
    from r2_object_storage.runtime_check import run_object_storage_runtime_check

    def factory(_settings: object, _credentials: object) -> RecordingClientSource:
        return source

    return await run_object_storage_runtime_check(
        ServiceName.WORKER,
        environ=environ,
        client_source_factory=factory,
        monotonic=lambda: 0.0,
        sleep=sleep if sleep is not None else _no_sleep,
        spool_janitor=spool_janitor
        if spool_janitor is not None
        else _ordering_janitor(order if order is not None else []),
    )


def _event_records(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    return [
        json.loads(line)
        for line in combined.splitlines()
        if line.startswith("{") and '"diagnostic_schema_version"' in line
    ]


def _combined_output(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def _assert_read_only(client: ScriptedS3Client, expected_head_bucket_calls: int) -> None:
    """The scripted runtime check only ever performs HeadBucket."""

    assert client.methods == ["head_bucket"] * expected_head_bucket_calls, client.methods
    assert client.put_requests == []
    assert client.get_calls == []


# --- CLI syntax: parsed before any environment or secret access ------------


def test_help_imports_and_runs_without_environment_secret_or_network_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(os, "getenv", _forbid_side_effect)
    monkeypatch.setattr(Path, "read_text", _forbid_side_effect)
    monkeypatch.setattr(socket, "create_connection", _forbid_side_effect)

    # Importing the entry module and resolving --help never touches the
    # environment, secret files or the network; the module's own import-light
    # structure is pinned separately by the AST import-discipline test below.
    module = importlib.import_module(COMMAND_MODULE)
    assert module.run(["--help"]) == 0
    assert COMMAND_PROGRAM in capsys.readouterr().out


def test_missing_service_argument_is_syntax_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getenv", _forbid_side_effect)
    from r2_object_storage.runtime_check import run

    assert run([]) == 2


def test_unknown_service_value_is_syntax_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getenv", _forbid_side_effect)
    from r2_object_storage.runtime_check import run

    assert run(["--service", "batch"]) == 2


def test_service_argument_accepts_exactly_the_three_existing_services() -> None:
    assert {member.value for member in ServiceName} == {"api", "mcp", "worker"}


def test_module_top_level_imports_are_standard_library_only() -> None:
    """The entry module stays import-light; all provider imports are lazy."""
    import ast

    module = importlib.import_module(COMMAND_MODULE)
    assert module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    non_stdlib = sorted(
        name for name in modules if name.split(".")[0] not in _STDLIB_TOP_LEVEL_PREFIXES
    )
    assert not non_stdlib, (
        f"{COMMAND_MODULE} must import only the standard library at module top "
        f"level; forbidden non-stdlib imports: {non_stdlib}"
    )


# --- configuration failures: exit 78, one emergency event, no probe --------


@pytest.mark.asyncio
async def test_configuration_failure_exits_78_without_touching_the_client(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    environ = {"KNOWLEDGE_SECRET_ROOT": str(tmp_path)}  # endpoint and bucket missing

    def _forbidden_factory(*_args: object, **_kwargs: object) -> RecordingClientSource:
        raise AssertionError("no client source may be built on a configuration failure")

    from r2_object_storage.runtime_check import run_object_storage_runtime_check

    exit_code = await run_object_storage_runtime_check(
        ServiceName.WORKER,
        environ=environ,
        client_source_factory=_forbidden_factory,
        monotonic=lambda: 0.0,
        sleep=_no_sleep,
        spool_janitor=_ordering_janitor([]),
    )

    assert exit_code == 78
    events = _event_records(capsys)
    assert len(events) == 1
    assert events[0]["event"] == "runtime_configuration_failed"
    assert events[0]["result_code"] == "failed"


@pytest.mark.asyncio
async def test_plaintext_secret_key_exits_78_and_never_renders_the_value(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()
    environ = _valid_environ(secret_root, spool_root)
    environ["KNOWLEDGE_R2_SECRET_ACCESS_KEY"] = "plaintext-secret-do-not-render"

    source = RecordingClientSource(ScriptedS3Client())
    exit_code = await _run_check(environ, source)

    assert exit_code == 78
    assert source.get_client_count == 0
    assert source.close_count == 0
    combined = _combined_output(capsys)
    assert "plaintext-secret-do-not-render" not in combined
    assert _ACCESS_KEY_SECRET_VALUE not in combined
    assert _SECRET_ACCESS_KEY_VALUE not in combined


# --- success: exit 0, one succeeded event, janitor before probe ------------


@pytest.mark.asyncio
async def test_success_exits_0_with_one_succeeded_event_and_one_close(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    client = ScriptedS3Client()
    client.enqueue(None)  # HeadBucket succeeds
    order: list[str] = []
    source = RecordingClientSource(client, order)
    exit_code = await _run_check(_valid_environ(secret_root, spool_root), source, order=order)

    assert exit_code == 0
    assert order == ["janitor", "get_client"]
    events = _event_records(capsys)
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "object_storage_operation_succeeded"
    assert event["result_code"] == "succeeded"
    assert event["operation"] == "head_bucket"
    assert event["provider"] == "r2"
    assert event["attempt_count"] == 1
    assert source.close_count == 1
    _assert_read_only(client, 1)

    combined = _combined_output(capsys)
    assert _VALID_ENDPOINT not in combined
    assert _VALID_BUCKET not in combined
    assert _ACCESS_KEY_SECRET_VALUE not in combined
    assert _SECRET_ACCESS_KEY_VALUE not in combined


@pytest.mark.asyncio
async def test_close_failure_after_success_keeps_exit_0_and_emits_closed_degradation(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Suppressing close failures would hide a degraded resource lifecycle."""

    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    client = ScriptedS3Client()
    client.enqueue(None)
    source = CloseFailingClientSource(client, RuntimeError("sentinel-close"))

    exit_code = await _run_check(_valid_environ(secret_root, spool_root), source)

    captured = capsys.readouterr()
    captured_output = captured.out + captured.err
    events = [
        json.loads(line)
        for line in captured_output.splitlines()
        if line.startswith("{") and '"diagnostic_schema_version"' in line
    ]
    assert exit_code == 0
    assert [event["event"] for event in events] == [
        "object_storage_operation_succeeded",
        "object_storage_client_close_degraded",
    ]
    assert events[-1]["operation"] == "object_storage_client_close"
    assert events[-1]["reason"] == "object_storage_client_close_failed"
    assert events[-1]["error_code"] == "internal_error"
    assert events[-1]["error_category"] == "internal"
    assert events[-1]["is_retryable"] is False
    assert source.close_count == 1
    assert "sentinel-close" not in captured_output
    _assert_read_only(client, 1)


# --- dependency and access failures: exit 69 -------------------------------


@pytest.mark.asyncio
async def test_access_denied_exits_69_without_retry(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    client = ScriptedS3Client()
    client.enqueue(_client_error("AccessDenied", 403))
    source = RecordingClientSource(client)
    sleep = SleepRecorder()
    exit_code = await _run_check(_valid_environ(secret_root, spool_root), source, sleep=sleep)

    assert exit_code == 69
    assert sleep.delays == []
    events = _event_records(capsys)
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "object_storage_operation_failed"
    assert event["error_code"] == "object_storage_access_denied"
    assert event["attempt_count"] == 1
    assert source.close_count == 1
    _assert_read_only(client, 1)


@pytest.mark.asyncio
async def test_close_failure_after_unavailable_probe_keeps_exit_69(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A close failure must not replace the already-determined probe exit code."""

    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    client = ScriptedS3Client()
    for _ in range(_EXPECTED_MAXIMUM_PROBE_ATTEMPTS):
        client.enqueue(_client_error("ServiceUnavailable", 503))
    source = CloseFailingClientSource(client, RuntimeError("sentinel-close"))

    exit_code = await _run_check(_valid_environ(secret_root, spool_root), source)

    captured = capsys.readouterr()
    captured_output = captured.out + captured.err
    events = [
        json.loads(line)
        for line in captured_output.splitlines()
        if line.startswith("{") and '"diagnostic_schema_version"' in line
    ]
    assert exit_code == 69
    assert {event["event"] for event in events} == {
        "object_storage_operation_failed",
        "object_storage_client_close_degraded",
    }
    close_event = next(
        event for event in events if event["event"] == "object_storage_client_close_degraded"
    )
    failed_event = next(
        event for event in events if event["event"] == "object_storage_operation_failed"
    )
    assert close_event["reason"] == "object_storage_client_close_failed"
    assert failed_event["error_code"] == "object_storage_unavailable"
    assert "sentinel-close" not in captured_output
    _assert_read_only(client, _EXPECTED_MAXIMUM_PROBE_ATTEMPTS)


@pytest.mark.asyncio
async def test_transient_provider_error_retries_bounded_then_exits_69(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    client = ScriptedS3Client()
    for _ in range(_EXPECTED_MAXIMUM_PROBE_ATTEMPTS):
        client.enqueue(_client_error("ServiceUnavailable", 503))
    source = RecordingClientSource(client)
    sleep = SleepRecorder()
    exit_code = await _run_check(_valid_environ(secret_root, spool_root), source, sleep=sleep)

    assert exit_code == 69
    assert len(sleep.delays) == _EXPECTED_MAXIMUM_PROBE_ATTEMPTS - 1
    events = _event_records(capsys)
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "object_storage_operation_failed"
    assert event["error_code"] == "object_storage_unavailable"
    assert event["attempt_count"] == _EXPECTED_MAXIMUM_PROBE_ATTEMPTS
    assert source.close_count == 1
    _assert_read_only(client, _EXPECTED_MAXIMUM_PROBE_ATTEMPTS)


@pytest.mark.asyncio
async def test_typed_unavailable_bucket_retries_bounded_then_exits_69(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    client = ScriptedS3Client()
    for _ in range(_EXPECTED_MAXIMUM_PROBE_ATTEMPTS):
        client.enqueue(ObjectStorageError(ErrorCode.OBJECT_STORAGE_UNAVAILABLE))
    source = RecordingClientSource(client)
    sleep = SleepRecorder()
    exit_code = await _run_check(_valid_environ(secret_root, spool_root), source, sleep=sleep)

    assert exit_code == 69
    assert len(sleep.delays) == _EXPECTED_MAXIMUM_PROBE_ATTEMPTS - 1
    events = _event_records(capsys)
    assert len(events) == 1
    assert events[0]["error_code"] == "object_storage_unavailable"
    assert events[0]["attempt_count"] == _EXPECTED_MAXIMUM_PROBE_ATTEMPTS
    assert source.close_count == 1
    _assert_read_only(client, _EXPECTED_MAXIMUM_PROBE_ATTEMPTS)


@pytest.mark.asyncio
async def test_transient_provider_error_recovers_within_the_bound(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    client = ScriptedS3Client()
    client.enqueue(_client_error("SlowDown", 503))
    client.enqueue(None)  # second HeadBucket attempt succeeds
    source = RecordingClientSource(client)
    sleep = SleepRecorder()
    exit_code = await _run_check(_valid_environ(secret_root, spool_root), source, sleep=sleep)

    assert exit_code == 0
    assert len(sleep.delays) == 1
    events = _event_records(capsys)
    assert len(events) == 1
    assert events[0]["event"] == "object_storage_operation_succeeded"
    assert events[0]["attempt_count"] == 2
    assert source.close_count == 1
    _assert_read_only(client, 2)


# --- unexpected internal failures: exit 70 ---------------------------------


@pytest.mark.asyncio
async def test_unexpected_client_failure_exits_70_without_rendering_the_cause(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    source = RaisingClientSource(RuntimeError("boom-do-not-render"))
    exit_code = await _run_check(_valid_environ(secret_root, spool_root), source)

    assert exit_code == 70
    events = _event_records(capsys)
    assert len(events) == 1
    assert events[0]["event"] == "internal_error"
    assert source.close_count == 1
    assert "boom-do-not-render" not in _combined_output(capsys)


@pytest.mark.asyncio
async def test_close_cancellation_propagates(
    tmp_path: Path,
) -> None:
    """Catching cancellation during close would leave task cancellation stuck."""

    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    client = ScriptedS3Client()
    client.enqueue(None)
    source = CloseFailingClientSource(client, asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _run_check(_valid_environ(secret_root, spool_root), source)

    assert source.close_count == 1
    _assert_read_only(client, 1)


@pytest.mark.asyncio
async def test_janitor_degradation_reports_safe_counts_but_never_skips_the_probe(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Janitor degradation is a warning, not a probe skip or an exit change.

    Spec §9.3 (a cleanup failure does not block unrelated reads; degraded
    candidates are handled by a later run) and §14.2 (the explicit runtime
    check calls read-only HeadBucket) both hold: the degraded event is emitted
    with safe counts, the probe still runs, and the exit code reflects only
    the probe outcome.
    """

    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    client = ScriptedS3Client()
    client.enqueue(None)  # probe succeeds despite the degraded janitor
    source = RecordingClientSource(client)

    async def _failing_janitor(_spool_root: Path) -> None:
        raise OSError("spool-scan-failed-do-not-render")

    exit_code = await _run_check(
        _valid_environ(secret_root, spool_root), source, spool_janitor=_failing_janitor
    )

    assert exit_code == 0
    assert source.get_client_count == 1
    assert source.close_count == 1
    _assert_read_only(client, 1)
    events = _event_records(capsys)
    assert [event["event"] for event in events] == [
        "object_storage_spool_cleanup_degraded",
        "object_storage_operation_succeeded",
    ]
    assert events[0]["operation"] == "spool_cleanup"
    assert events[0]["count"] == 0
    assert events[0]["reason"] == "spool_cleanup_janitor_failed"
    assert events[1]["attempt_count"] == 1
    combined = _combined_output(capsys)
    assert "spool-scan-failed-do-not-render" not in combined


@pytest.mark.asyncio
async def test_janitor_degradation_does_not_mask_a_probe_failure(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """With a degraded janitor and an access-denied probe, the exit is 69."""

    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    client = ScriptedS3Client()
    client.enqueue(_client_error("AccessDenied", 403))
    source = RecordingClientSource(client)

    async def _failing_janitor(_spool_root: Path) -> None:
        raise OSError("spool-scan-failed-do-not-render")

    exit_code = await _run_check(
        _valid_environ(secret_root, spool_root), source, spool_janitor=_failing_janitor
    )

    assert exit_code == 69
    assert source.close_count == 1
    _assert_read_only(client, 1)
    events = _event_records(capsys)
    assert [event["event"] for event in events] == [
        "object_storage_spool_cleanup_degraded",
        "object_storage_operation_failed",
    ]
    assert events[1]["error_code"] == "object_storage_access_denied"
    assert "spool-scan-failed-do-not-render" not in _combined_output(capsys)


@pytest.mark.asyncio
async def test_janitor_deferred_candidates_emit_degraded_with_real_count(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Spec §9.3: candidates beyond the per-run bound emit the real count.

    A successful janitor that deferred three candidates emits
    ``object_storage_spool_cleanup_degraded`` with ``count == 3`` while the
    probe still runs and the exit code still reflects only the probe outcome.
    """

    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    client = ScriptedS3Client()
    client.enqueue(None)
    source = RecordingClientSource(client)

    def _deferred_janitor(_spool_root: Path) -> Awaitable[SpoolCleanupSummary]:
        from r2_object_storage.spool import SpoolCleanupSummary

        async def _janitor() -> SpoolCleanupSummary:
            from r2_object_storage.spool import SPOOL_CLEANUP_DEFERRED

            return SpoolCleanupSummary(1_000, 997, 0, 3, reason=SPOOL_CLEANUP_DEFERRED)

        return _janitor()

    exit_code = await _run_check(
        _valid_environ(secret_root, spool_root), source, spool_janitor=_deferred_janitor
    )

    assert exit_code == 0
    assert source.get_client_count == 1
    assert source.close_count == 1
    _assert_read_only(client, 1)
    events = _event_records(capsys)
    assert [event["event"] for event in events] == [
        "object_storage_spool_cleanup_degraded",
        "object_storage_operation_succeeded",
    ]
    assert events[0]["operation"] == "spool_cleanup"
    assert events[0]["count"] == 3
    assert events[0]["reason"] == "spool_cleanup_deferred"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_count", "reason_value"),
    [
        (0, "spool_cleanup_scan_failed"),
        (1, "spool_cleanup_entry_failed"),
    ],
)
async def test_janitor_summary_failure_reason_is_emitted(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    failed_count: int,
    reason_value: str,
) -> None:
    """Discarding a summary reason would prevent operators distinguishing cleanup failures."""

    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    client = ScriptedS3Client()
    client.enqueue(None)
    source = RecordingClientSource(client)

    def _reasoned_janitor(_spool_root: Path) -> Awaitable[SpoolCleanupSummary]:
        from r2_object_storage.spool import (
            SPOOL_CLEANUP_ENTRY_FAILED,
            SPOOL_CLEANUP_SCAN_FAILED,
            SpoolCleanupSummary,
        )

        reason = {
            "spool_cleanup_scan_failed": SPOOL_CLEANUP_SCAN_FAILED,
            "spool_cleanup_entry_failed": SPOOL_CLEANUP_ENTRY_FAILED,
        }[reason_value]

        async def _janitor() -> SpoolCleanupSummary:
            return SpoolCleanupSummary(
                failed_count,
                0,
                0,
                0,
                failed_count,
                reason,
            )

        return _janitor()

    exit_code = await _run_check(
        _valid_environ(secret_root, spool_root), source, spool_janitor=_reasoned_janitor
    )

    assert exit_code == 0
    events = _event_records(capsys)
    assert [event["event"] for event in events] == [
        "object_storage_spool_cleanup_degraded",
        "object_storage_operation_succeeded",
    ]
    assert events[0]["reason"] == reason_value
    _assert_read_only(client, 1)


@pytest.mark.asyncio
async def test_janitor_without_deferred_candidates_emits_no_cleanup_event(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A fully successful janitor keeps the clean-success one-event invariant."""

    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    _secret_files(secret_root)
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    client = ScriptedS3Client()
    client.enqueue(None)
    source = RecordingClientSource(client)

    def _clean_janitor(_spool_root: Path) -> Awaitable[SpoolCleanupSummary]:
        from r2_object_storage.spool import SpoolCleanupSummary

        async def _janitor() -> SpoolCleanupSummary:
            return SpoolCleanupSummary(5, 5, 0, 0)

        return _janitor()

    exit_code = await _run_check(
        _valid_environ(secret_root, spool_root), source, spool_janitor=_clean_janitor
    )

    assert exit_code == 0
    assert source.close_count == 1
    _assert_read_only(client, 1)
    events = _event_records(capsys)
    assert [event["event"] for event in events] == ["object_storage_operation_succeeded"]
