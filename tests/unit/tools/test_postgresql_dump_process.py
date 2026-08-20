"""Bounded pg_dump/pg_restore process adapter contract (spec 4.3, 9.3, 11.2, 17).

Every test injects a scripted runner so no PostgreSQL client is ever spawned.
The tests pin the exact semantic argument vectors, the credential boundary
(one ephemeral mode-0600 PGPASSFILE, never a password-bearing environment
variable), the fail-closed version gate and the closed-token error mapping
that never carries raw stderr.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).parents[3]))

from tools import postgresql_dump_process as postgresql_dump_process_module
from tools.postgresql_dump_process import (
    CHILD_TERMINATE_GRACE_SECONDS,
    EXPECTED_POSTGRESQL_CLIENT_VERSION,
    PostgresqlDumpProcessAdapter,
    ProcessRunResult,
    check_client_tools,
    parse_client_version,
    resolve_client_tool,
    run_bounded_child,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.recovery.contracts import RecoveryDependency, RecoveryError
from personal_os.recovery.ports import PostgresqlConnectionTarget

_SNAPSHOT_TOKEN = "00000003-0000001D-1"
_HOST = "127.0.0.1"
_PORT = 5432
_USER = "knowledge_app"
_DATABASE = "knowledge"
_PASSWORD = "sentinel-password-value"
_TARGET = PostgresqlConnectionTarget(host=_HOST, port=_PORT, database=_DATABASE, user=_USER)
#: Sentinel that must never surface in any raised error text.
_STDERR_SENTINEL = "secret-stderr-detail-must-never-leak"
_PROBE_OUTPUT_DIGEST = "cc9282bc6e2a067298726aacac0d6c3aba5fe1e74ca5bbdf8f47f24d8b31c272"
_DEFAULT_PASSFILE_DIGEST = "fa97d2a442f0c239446b62ec331eed57670a366e8b67b1de739e9f6ce3b5a200"
_ESCAPED_PASSFILE_DIGEST = "8cbbc0c702e508b1a3d6d07ca9fec995712de1ce74cbfc48a53a623998d42a1f"

_EXPECTED_DUMP_ARGUMENTS = [
    "--format=custom",
    "--no-owner",
    "--no-privileges",
    "--no-password",
    "--lock-wait-timeout=15000",
    f"--snapshot={_SNAPSHOT_TOKEN}",
    "--file={output}",
    "--host",
    _HOST,
    "--port",
    str(_PORT),
    "--username",
    _USER,
    _DATABASE,
]

_EXPECTED_RESTORE_ARGUMENTS = [
    "--single-transaction",
    "--exit-on-error",
    "--no-owner",
    "--no-privileges",
    "--no-password",
    "--host",
    _HOST,
    "--port",
    str(_PORT),
    "--username",
    _USER,
    "--dbname",
    _DATABASE,
    "{input}",
]


@dataclass(frozen=True, slots=True)
class RecordedCall:
    argv: tuple[str, ...]
    env: dict[str, str]
    timeout_seconds: float
    passfile_exists_at_call: bool | None = None
    passfile_digest_at_call: str | None = None
    passfile_mode_at_call: int | None = None


class ScriptedRunner:
    """Fake bounded runner that records calls and snapshots the passfile state."""

    def __init__(
        self,
        *,
        results: Sequence[ProcessRunResult] | None = None,
        error: BaseException | None = None,
        default_result: ProcessRunResult | None = None,
    ) -> None:
        self.calls: list[RecordedCall] = []
        self._results = list(results) if results is not None else None
        self._error = error
        self._default_result = default_result or ProcessRunResult(returncode=0)

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> ProcessRunResult:
        passfile_text = env.get("PGPASSFILE")
        passfile_exists = Path(passfile_text).is_file() if passfile_text else None
        passfile_content = (
            Path(passfile_text).read_text(encoding="utf-8") if passfile_exists else None
        )
        passfile_mode = (
            stat.S_IMODE(Path(passfile_text).stat().st_mode) if passfile_exists else None
        )
        self.calls.append(
            RecordedCall(
                argv=tuple(argv),
                env=dict(env),
                timeout_seconds=timeout_seconds,
                passfile_exists_at_call=passfile_exists,
                passfile_digest_at_call=(
                    hashlib.sha256(passfile_content.encode("utf-8")).hexdigest()
                    if passfile_content is not None
                    else None
                ),
                passfile_mode_at_call=passfile_mode,
            )
        )
        if self._error is not None:
            raise self._error
        if self._results is not None:
            return self._results.pop(0)
        return self._default_result


def _adapter(runner: ScriptedRunner) -> PostgresqlDumpProcessAdapter:
    return PostgresqlDumpProcessAdapter(
        dump_binary="pg-dump-binary",
        restore_binary="pg-restore-binary",
        password=SecretStr(_PASSWORD),
        runner=runner,
    )


def _assert_error_is_closed_and_sentinel_free(error: RecoveryError) -> None:
    rendered = repr(error) + str(error) + repr(error.to_safe_dict())
    if _STDERR_SENTINEL in rendered:
        pytest.fail("closed recovery error rendered protected child output")
    if _PASSWORD in rendered:
        pytest.fail("closed recovery error rendered a protected credential")


def _opaque_text_digest(value: str) -> str:
    """Return a safe assertion value for protected process-boundary fixtures."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _assert_no_protected_rendering(rendered: str, protected_values: tuple[str, ...]) -> None:
    """Fail without making pytest/JUnit render a protected fixture value."""
    if any(value in rendered for value in protected_values):
        pytest.fail("recovery boundary rendered a protected fixture value")


class DrainTrackingStream:
    """Minimal async pipe fake that proves cancellation drains both streams."""

    def __init__(self) -> None:
        self.read_count = 0

    async def read(self, _limit: int) -> bytes:
        self.read_count += 1
        return b""


class CancellationCleanupProcess:
    """Child fake that exits only after terminate-then-kill cleanup."""

    def __init__(self) -> None:
        self.stdout = DrainTrackingStream()
        self.stderr = DrainTrackingStream()
        self.returncode: int | None = None
        self.wait_started = asyncio.Event()
        self._exited = asyncio.Event()
        self.wait_count = 0
        self.terminate_count = 0
        self.kill_count = 0

    async def wait(self) -> int:
        self.wait_count += 1
        self.wait_started.set()
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminate_count += 1

    def kill(self) -> None:
        self.kill_count += 1
        self.returncode = -9
        self._exited.set()


class BlockingDrainTrackingStream:
    """Pipe fake that distinguishes a completed drain from cancellation."""

    def __init__(self, release: asyncio.Event) -> None:
        self._release = release
        self.read_started = asyncio.Event()
        self.drain_completed = False
        self.was_cancelled = False

    async def read(self, _limit: int) -> bytes:
        self.read_started.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise
        self.drain_completed = True
        return b""


class ExitedProcessWithBlockingPipes:
    """Already-exited child whose pipes still need their EOF drains awaited."""

    def __init__(self) -> None:
        self.returncode = 0
        self.wait_started = asyncio.Event()
        self._release_pipes = asyncio.Event()
        self.stdout = BlockingDrainTrackingStream(self._release_pipes)
        self.stderr = BlockingDrainTrackingStream(self._release_pipes)

    async def wait(self) -> int:
        self.wait_started.set()
        return self.returncode

    def release_pipes(self) -> None:
        self._release_pipes.set()


@pytest.mark.asyncio
async def test_create_dump_uses_exact_semantic_argument_vector(tmp_path: Path) -> None:
    runner = ScriptedRunner()
    output_file = tmp_path / "postgres.dump"
    output_file.write_bytes(b"dump-bytes")
    adapter = _adapter(runner)

    await adapter.create_dump(_SNAPSHOT_TOKEN, output_file, _TARGET, timeout_seconds=321.0)

    assert len(runner.calls) == 1
    call = runner.calls[0]
    expected = ["pg-dump-binary", *_EXPECTED_DUMP_ARGUMENTS]
    assert _opaque_text_digest("\x00".join(call.argv)) == _opaque_text_digest(
        "\x00".join(argument.format(output=str(output_file)) for argument in expected)
    )
    assert call.timeout_seconds == 321.0


@pytest.mark.asyncio
async def test_create_dump_default_timeout_is_ten_minutes(tmp_path: Path) -> None:
    runner = ScriptedRunner()
    output_file = tmp_path / "postgres.dump"
    output_file.write_bytes(b"dump-bytes")

    await _adapter(runner).create_dump(_SNAPSHOT_TOKEN, output_file, _TARGET)

    assert runner.calls[0].timeout_seconds == 600.0


@pytest.mark.asyncio
async def test_restore_dump_uses_exact_semantic_argument_vector(tmp_path: Path) -> None:
    runner = ScriptedRunner()
    input_file = tmp_path / "restore.dump"
    input_file.write_bytes(b"dump-bytes")

    await _adapter(runner).restore_dump(input_file, _TARGET, timeout_seconds=123.0)

    assert len(runner.calls) == 1
    call = runner.calls[0]
    expected = ["pg-restore-binary", *_EXPECTED_RESTORE_ARGUMENTS]
    assert list(call.argv) == [argument.format(input=str(input_file)) for argument in expected]
    assert call.timeout_seconds == 123.0


@pytest.mark.asyncio
async def test_child_env_sets_only_pgpassfile_and_never_password_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGPASSWORD", _STDERR_SENTINEL)
    monkeypatch.setenv("PGHOST", "env-host-must-not-apply")
    monkeypatch.setenv("PGCLIENTENCODING", "UTF8")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:env-password@env/db")
    output_file = tmp_path / "postgres.dump"
    output_file.write_bytes(b"dump-bytes")
    runner = ScriptedRunner()

    await _adapter(runner).create_dump(_SNAPSHOT_TOKEN, output_file, _TARGET)

    child_env_keys = frozenset(runner.calls[0].env)
    assert "PGPASSFILE" in child_env_keys
    assert "PGPASSWORD" not in child_env_keys
    assert "DATABASE_URL" not in child_env_keys
    leaked_pg_keys = {
        key for key in child_env_keys if key.startswith("PG") and key != "PGPASSFILE"
    }
    assert leaked_pg_keys == set()
    # Non-PG environment still reaches the child (libpq needs PATH to resolve).
    assert "PATH" in child_env_keys


@pytest.mark.asyncio
async def test_passfile_is_ephemeral_outside_bundle_and_removed_in_finally(
    tmp_path: Path,
) -> None:
    output_file = tmp_path / "postgres.dump"
    output_file.write_bytes(b"dump-bytes")
    runner = ScriptedRunner()

    await _adapter(runner).create_dump(_SNAPSHOT_TOKEN, output_file, _TARGET)

    call = runner.calls[0]
    passfile_path = Path(call.env["PGPASSFILE"])
    assert call.passfile_exists_at_call is True
    assert call.passfile_digest_at_call == _DEFAULT_PASSFILE_DIGEST
    assert passfile_path.parent == Path(tempfile.gettempdir())
    if os.name == "posix":
        assert call.passfile_mode_at_call == 0o600
    assert not passfile_path.exists()


@pytest.mark.asyncio
async def test_passfile_removed_even_when_runner_raises(tmp_path: Path) -> None:
    output_file = tmp_path / "postgres.dump"
    output_file.write_bytes(b"dump-bytes")
    runner = ScriptedRunner(error=RuntimeError(_STDERR_SENTINEL))

    with pytest.raises(RecoveryError) as raised:
        await _adapter(runner).create_dump(_SNAPSHOT_TOKEN, output_file, _TARGET)

    _assert_error_is_closed_and_sentinel_free(raised.value)
    passfile_path = Path(runner.calls[0].env["PGPASSFILE"])
    assert not passfile_path.exists()


@pytest.mark.asyncio
async def test_passfile_removed_on_cancellation(tmp_path: Path) -> None:
    output_file = tmp_path / "postgres.dump"
    output_file.write_bytes(b"dump-bytes")
    runner = ScriptedRunner(error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _adapter(runner).create_dump(_SNAPSHOT_TOKEN, output_file, _TARGET)

    passfile_path = Path(runner.calls[0].env["PGPASSFILE"])
    assert not passfile_path.exists()


def test_missing_binary_fails_closed_as_dependency_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tools.postgresql_dump_process.shutil.which", lambda name: None)

    with pytest.raises(RecoveryError) as raised:
        resolve_client_tool("pg_dump")

    error = raised.value
    assert error.error_code is ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE
    assert error.safe_details["dependency"] is RecoveryDependency.PG_CLIENT


@pytest.mark.asyncio
async def test_client_version_exact_expected_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tools.postgresql_dump_process.shutil.which",
        lambda tool_name: f"/resolved/{tool_name}",
    )
    runner = ScriptedRunner(
        default_result=ProcessRunResult(
            returncode=0, stdout=f"pg_dump (PostgreSQL) {EXPECTED_POSTGRESQL_CLIENT_VERSION}\n"
        )
    )

    await check_client_tools("pg_dump", "pg_restore", runner=runner)

    assert len(runner.calls) == 2
    assert runner.calls[0].argv == (str(Path("/resolved/pg_dump")), "--version")
    assert runner.calls[1].argv == (str(Path("/resolved/pg_restore")), "--version")
    for call in runner.calls:
        assert call.timeout_seconds > 0
        assert call.env.get("PGPASSFILE") is None
        assert not any(key.startswith("PG") for key in call.env)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["17.4", "18.3", "19.1", "18.4.1"])
async def test_client_version_mismatch_fails_closed(version: str) -> None:
    runner = ScriptedRunner(
        default_result=ProcessRunResult(returncode=0, stdout=f"pg_restore (PostgreSQL) {version}\n")
    )

    with pytest.raises(RecoveryError) as raised:
        await check_client_tools("pg_dump", "pg_restore", runner=runner)

    error = raised.value
    assert error.error_code is ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE
    assert error.safe_details["dependency"] is RecoveryDependency.PG_CLIENT


@pytest.mark.asyncio
async def test_unparseable_version_output_rejected() -> None:
    runner = ScriptedRunner(
        default_result=ProcessRunResult(returncode=0, stdout="some unexpected tool text\n")
    )

    with pytest.raises(RecoveryError) as raised:
        await check_client_tools("pg_dump", "pg_restore", runner=runner)

    assert raised.value.error_code is ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE
    assert raised.value.safe_details["dependency"] is RecoveryDependency.PG_CLIENT


@pytest.mark.asyncio
async def test_version_probe_nonzero_exit_fails_closed() -> None:
    runner = ScriptedRunner(default_result=ProcessRunResult(returncode=1, timed_out=False))

    with pytest.raises(RecoveryError) as raised:
        await check_client_tools("pg_dump", "pg_restore", runner=runner)

    assert raised.value.error_code is ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE


def test_parse_client_version_extracts_patch_version() -> None:
    assert parse_client_version("pg_dump (PostgreSQL) 18.4\n") == "18.4"
    assert parse_client_version("pg_restore (PostgreSQL) 18.4") == "18.4"


def test_parse_client_version_rejects_unparseable_output() -> None:
    with pytest.raises(ValueError, match="unparseable"):
        parse_client_version("some unexpected tool text")


def test_process_result_repr_excludes_captured_stdout() -> None:
    rendered = repr(ProcessRunResult(returncode=0, stdout="captured-output"))
    _assert_no_protected_rendering(rendered, ("captured-output",))


@pytest.mark.asyncio
async def test_passfile_escapes_libpq_delimiters_in_every_field() -> None:
    adapter = PostgresqlDumpProcessAdapter(
        dump_binary="pg-dump-binary",
        restore_binary="pg-restore-binary",
        password=SecretStr("pass\\word"),
    )
    target = PostgresqlConnectionTarget(
        host="host:name",
        port=5432,
        database="database\\name",
        user="user:name",
    )

    async with adapter._ephemeral_passfile(adapter._build_passfile_line(target)) as passfile:
        passfile_digest = _opaque_text_digest(passfile.read_text(encoding="utf-8"))
        assert passfile_digest == _ESCAPED_PASSFILE_DIGEST


@pytest.mark.asyncio
async def test_dump_rejects_passfile_line_breaks_before_spawning(tmp_path: Path) -> None:
    invalid_targets = (
        PostgresqlConnectionTarget(
            host=f"host{chr(10)}name", port=5432, database="knowledge", user="knowledge_app"
        ),
        PostgresqlConnectionTarget(
            host="127.0.0.1", port=5432, database=f"knowledge{chr(13)}name", user="knowledge_app"
        ),
    )
    output_file = tmp_path / "postgres.dump"
    output_file.write_bytes(b"dump-bytes")
    for target in invalid_targets:
        runner = ScriptedRunner()

        with pytest.raises(RecoveryError) as raised:
            await _adapter(runner).create_dump(_SNAPSHOT_TOKEN, output_file, target)

        error = raised.value
        assert error.error_code is ErrorCode.CANONICAL_RECOVERY_INTEGRITY_FAILED
        if runner.calls:
            pytest.fail("dump runner was called for an invalid password-file field")
        _assert_no_protected_rendering(
            repr(error) + str(error) + repr(error.to_safe_dict()),
            (target.host, target.database, target.user, _PASSWORD, _SNAPSHOT_TOKEN),
        )


@pytest.mark.asyncio
async def test_restore_rejects_passfile_line_break_in_password_before_spawning(
    tmp_path: Path,
) -> None:
    input_file = tmp_path / "restore.dump"
    input_file.write_bytes(b"dump-bytes")
    invalid_password = f"password{chr(10)}value"
    runner = ScriptedRunner()
    adapter = PostgresqlDumpProcessAdapter(
        dump_binary="pg-dump-binary",
        restore_binary="pg-restore-binary",
        password=SecretStr(invalid_password),
        runner=runner,
    )

    with pytest.raises(RecoveryError) as raised:
        await adapter.restore_dump(input_file, _TARGET)

    error = raised.value
    assert error.error_code is ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED
    if runner.calls:
        pytest.fail("restore runner was called for an invalid password-file field")
    _assert_no_protected_rendering(
        repr(error) + str(error) + repr(error.to_safe_dict()),
        (_TARGET.host, _TARGET.database, _TARGET.user, invalid_password),
    )


@pytest.mark.asyncio
async def test_dump_failure_maps_to_integrity_failed_without_raw_stderr(
    tmp_path: Path,
) -> None:
    output_file = tmp_path / "postgres.dump"
    output_file.write_bytes(b"partial-bytes")
    runner = ScriptedRunner(default_result=ProcessRunResult(returncode=1, stdout=_STDERR_SENTINEL))

    with pytest.raises(RecoveryError) as raised:
        await _adapter(runner).create_dump(_SNAPSHOT_TOKEN, output_file, _TARGET)

    error = raised.value
    assert error.error_code is ErrorCode.CANONICAL_RECOVERY_INTEGRITY_FAILED
    assert error.safe_details["component"] == "postgres_dump"
    _assert_error_is_closed_and_sentinel_free(error)


@pytest.mark.asyncio
async def test_restore_failure_maps_to_restore_failed(tmp_path: Path) -> None:
    input_file = tmp_path / "restore.dump"
    input_file.write_bytes(b"dump-bytes")
    runner = ScriptedRunner(default_result=ProcessRunResult(returncode=1, stdout=_STDERR_SENTINEL))

    with pytest.raises(RecoveryError) as raised:
        await _adapter(runner).restore_dump(input_file, _TARGET)

    error = raised.value
    assert error.error_code is ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED
    assert error.safe_details["component"] == "postgres_restore"
    _assert_error_is_closed_and_sentinel_free(error)


@pytest.mark.asyncio
async def test_dump_runner_exception_maps_to_integrity_failed(
    tmp_path: Path,
) -> None:
    output_file = tmp_path / "postgres.dump"
    output_file.write_bytes(b"dump-bytes")
    runner = ScriptedRunner(error=RuntimeError(_STDERR_SENTINEL))

    with pytest.raises(RecoveryError) as raised:
        await _adapter(runner).create_dump(_SNAPSHOT_TOKEN, output_file, _TARGET)

    error = raised.value
    assert error.error_code is ErrorCode.CANONICAL_RECOVERY_INTEGRITY_FAILED
    assert error.safe_details["component"] == "postgres_dump"
    _assert_error_is_closed_and_sentinel_free(error)


@pytest.mark.asyncio
async def test_restore_runner_exception_maps_to_restore_failed(
    tmp_path: Path,
) -> None:
    input_file = tmp_path / "restore.dump"
    input_file.write_bytes(b"dump-bytes")
    runner = ScriptedRunner(error=RuntimeError(_STDERR_SENTINEL))

    with pytest.raises(RecoveryError) as raised:
        await _adapter(runner).restore_dump(input_file, _TARGET)

    error = raised.value
    # A restore-side spawn failure must carry the restore component, never the
    # dump-side integrity token.
    assert error.error_code is ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED
    assert error.safe_details["component"] == "postgres_restore"
    _assert_error_is_closed_and_sentinel_free(error)


@pytest.mark.asyncio
async def test_dump_timeout_maps_to_integrity_failed(tmp_path: Path) -> None:
    output_file = tmp_path / "postgres.dump"
    output_file.write_bytes(b"partial-bytes")
    runner = ScriptedRunner(default_result=ProcessRunResult(returncode=-1, timed_out=True))

    with pytest.raises(RecoveryError) as raised:
        await _adapter(runner).create_dump(_SNAPSHOT_TOKEN, output_file, _TARGET)

    error = raised.value
    assert error.error_code is ErrorCode.CANONICAL_RECOVERY_INTEGRITY_FAILED
    assert error.safe_details["component"] == "postgres_dump"


@pytest.mark.asyncio
async def test_restore_timeout_maps_to_restore_failed(tmp_path: Path) -> None:
    input_file = tmp_path / "restore.dump"
    input_file.write_bytes(b"dump-bytes")
    runner = ScriptedRunner(default_result=ProcessRunResult(returncode=-1, timed_out=True))

    with pytest.raises(RecoveryError) as raised:
        await _adapter(runner).restore_dump(input_file, _TARGET)

    assert raised.value.error_code is ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED


@pytest.mark.asyncio
async def test_dump_receipt_hashes_exact_output_file(tmp_path: Path) -> None:
    payload = b"canonical dump payload" * 100_000  # exercises the streaming path
    output_file = tmp_path / "postgres.dump"
    output_file.write_bytes(payload)
    decoy_file = tmp_path / "other.dump"
    decoy_file.write_bytes(b"must not be hashed")
    runner = ScriptedRunner()

    receipt = await _adapter(runner).create_dump(_SNAPSHOT_TOKEN, output_file, _TARGET)

    assert receipt.size_bytes == len(payload)
    assert receipt.sha256 == hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_restore_success_returns_completion_timestamp(tmp_path: Path) -> None:
    input_file = tmp_path / "restore.dump"
    input_file.write_bytes(b"dump-bytes")
    before = datetime.now(UTC)
    runner = ScriptedRunner()

    receipt = await _adapter(runner).restore_dump(input_file, _TARGET)

    assert before <= receipt.completed_at <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_no_shell_invocation_anywhere(tmp_path: Path) -> None:
    output_file = tmp_path / "postgres.dump"
    output_file.write_bytes(b"dump-bytes")
    runner = ScriptedRunner()
    adapter = _adapter(runner)

    await adapter.create_dump(_SNAPSHOT_TOKEN, output_file, _TARGET)
    await adapter.restore_dump(output_file, _TARGET)

    assert runner.calls
    for call in runner.calls:
        if isinstance(call.argv, str):
            pytest.fail("child runner received a shell command string")
        if not isinstance(call.argv, tuple) or not all(
            isinstance(argument, str) for argument in call.argv
        ):
            pytest.fail("child runner received a non-string argument vector")


@pytest.mark.asyncio
async def test_run_bounded_child_returns_success_without_shell() -> None:
    result = await run_bounded_child(
        [sys.executable, "-c", "print('probe')"],
        env=dict(os.environ),
        timeout_seconds=30.0,
    )

    assert result.returncode == 0
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_run_bounded_child_returns_drained_capped_stdout() -> None:
    result = await run_bounded_child(
        [sys.executable, "-c", "print('probe-output')"],
        env=dict(os.environ),
        timeout_seconds=30.0,
    )

    # The capped stdout is the client-version gate's only input, so the
    # bounded runner must return what it drained from the child.
    assert result.returncode == 0
    assert _opaque_text_digest(result.stdout.strip()) == _PROBE_OUTPUT_DIGEST


@pytest.mark.asyncio
async def test_run_bounded_child_drains_both_pipes_before_child_exit() -> None:
    result = await run_bounded_child(
        [
            sys.executable,
            "-c",
            "import sys; "
            "sys.stdout.write('x' * 131072); sys.stdout.flush(); "
            "sys.stderr.write('y' * 131072); sys.stderr.flush()",
        ],
        env=dict(os.environ),
        timeout_seconds=5.0,
    )

    assert result.returncode == 0
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_run_bounded_child_terminates_then_kills_within_grace() -> None:
    started_monotonic = time.monotonic()

    result = await run_bounded_child(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=dict(os.environ),
        timeout_seconds=0.5,
    )

    elapsed_seconds = time.monotonic() - started_monotonic
    assert result.timed_out is True
    assert result.returncode != 0
    assert elapsed_seconds < 0.5 + CHILD_TERMINATE_GRACE_SECONDS + 10.0


@pytest.mark.asyncio
async def test_run_bounded_child_cancellation_terminates_kills_and_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = CancellationCleanupProcess()

    async def create_subprocess_exec(
        *_args: object, **_kwargs: object
    ) -> CancellationCleanupProcess:
        return process

    monkeypatch.setattr(
        postgresql_dump_process_module.asyncio, "create_subprocess_exec", create_subprocess_exec
    )
    monkeypatch.setattr(postgresql_dump_process_module, "CHILD_TERMINATE_GRACE_SECONDS", 0.0)
    running = asyncio.create_task(
        run_bounded_child(["controlled-child"], env={}, timeout_seconds=60.0)
    )
    await asyncio.wait_for(process.wait_started.wait(), timeout=1.0)
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running

    assert process.terminate_count == 1
    assert process.kill_count == 1
    assert process.wait_count >= 2
    assert process.stdout.read_count > 0
    assert process.stderr.read_count > 0


@pytest.mark.asyncio
async def test_run_bounded_child_cancellation_finishes_pipe_drains_after_child_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = ExitedProcessWithBlockingPipes()

    async def create_subprocess_exec(
        *_args: object, **_kwargs: object
    ) -> ExitedProcessWithBlockingPipes:
        return process

    monkeypatch.setattr(
        postgresql_dump_process_module.asyncio, "create_subprocess_exec", create_subprocess_exec
    )
    running = asyncio.create_task(
        run_bounded_child(["controlled-child"], env={}, timeout_seconds=60.0)
    )
    await asyncio.wait_for(process.wait_started.wait(), timeout=1.0)
    await asyncio.wait_for(
        asyncio.gather(process.stdout.read_started.wait(), process.stderr.read_started.wait()),
        timeout=1.0,
    )
    running.cancel()
    asyncio.get_running_loop().call_soon(process.release_pipes)

    with pytest.raises(asyncio.CancelledError):
        await running

    assert process.stdout.drain_completed is True
    assert process.stderr.drain_completed is True
    if process.stdout.was_cancelled or process.stderr.was_cancelled:
        pytest.fail("caller cancellation cancelled a child pipe drain")
