"""Bounded ``pg_dump``/``pg_restore`` subprocess adapter for canonical recovery.

Implements the :class:`personal_os.recovery.ports.PostgresqlDumpProcess` port
behind one fail-closed subprocess boundary (design spec 4.3, 9.3, 11.2, 17):

- exact semantic argument vectors, never a shell, never a connection string,
  never parallel jobs, never stdout archive streaming;
- credentials travel only through an ephemeral mode-0600 libpq password file
  in the system temp directory — never ``DATABASE_URL``, ``PGPASSWORD``, a
  password-bearing DSN or a CLI flag;
- the child environment is ``os.environ`` minus every ``PG*`` key and
  ``DATABASE_URL``, plus exactly ``PGPASSFILE``;
- stderr is drained and discarded; command arguments, the child environment
  and raw subprocess output are never logged, raised or attached to errors;
- both binaries are version-gated against the pinned client version before
  any snapshot is acquired, and every child runs under a bounded timeout with
  terminate-then-kill escalation.

This module composes subprocesses, which is exactly why it lives in ``tools/``
and never in the import-clean core packages.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from pydantic import SecretStr

from personal_os.error_contracts.codes import ErrorCode
from personal_os.recovery.contracts import (
    RecoveryComponent,
    RecoveryDependency,
    RecoveryError,
)
from personal_os.recovery.ports import (
    DumpReceipt,
    PostgresqlConnectionTarget,
    RestoreReceipt,
)

#: The only accepted PostgreSQL client version (spec 4.3), compared exactly.
EXPECTED_POSTGRESQL_CLIENT_VERSION: Final[str] = "18.4"

#: Fixed grace between terminate and kill when a child exceeds its bound.
CHILD_TERMINATE_GRACE_SECONDS: Final[float] = 5.0

#: Bound for one ``--version`` probe through the same bounded runner.
VERSION_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0

#: Receipt hashing streams the dump in chunks of this size, never whole files.
_HASH_CHUNK_SIZE_BYTES: Final[int] = 1024 * 1024

#: Upper bound on captured ``--version`` stdout; everything else is discarded.
_MAX_CAPTURE_BYTES: Final[int] = 8192

#: ``pg_dump (PostgreSQL) 18.4``-style probe output; anything else fails closed.
_CLIENT_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"pg_\w+ \(PostgreSQL\) (\d+(?:\.\d+)+)"
)

#: libpq passfile permissions; POSIX-only, creation is already private elsewhere.
_PASSFILE_MODE_POSIX: Final[int] = 0o600


@dataclass(frozen=True, slots=True)
class ProcessRunResult:
    """Bounded child outcome: exit status, timeout flag and capped stdout.

    Raw stderr never appears here — it is drained and discarded by the runner.
    The capped ``stdout`` exists only so the version gate can parse
    ``<binary> --version`` output through the same bounded runner.
    """

    returncode: int
    timed_out: bool = False
    stdout: str = ""


class BoundedChildRunner(Protocol):
    """Callable contract for one bounded, shell-free child execution."""

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> ProcessRunResult: ...


async def run_bounded_child(
    argv: Sequence[str], *, env: Mapping[str, str], timeout_seconds: float
) -> ProcessRunResult:
    """Run one child without a shell; stderr is consumed and never forwarded."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=dict(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        process.terminate()
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=CHILD_TERMINATE_GRACE_SECONDS)
        if process.returncode is None:
            process.kill()
            await process.wait()
        await _drain(process)
        exit_code = process.returncode if process.returncode is not None else -1
        return ProcessRunResult(returncode=exit_code, timed_out=True)
    await _drain(process)
    exit_code = process.returncode if process.returncode is not None else -1
    return ProcessRunResult(returncode=exit_code)


async def _drain(process: asyncio.subprocess.Process) -> str:
    """Drain both pipes, returning only capped stdout and discarding stderr."""
    capped_stdout = ""
    if process.stdout is not None:
        raw_stdout = await process.stdout.read(_MAX_CAPTURE_BYTES + 1)
        capped_stdout = raw_stdout[:_MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")
    if process.stderr is not None:
        await process.stderr.read()
    return capped_stdout


def parse_client_version(output: str) -> str:
    """Extract the client version from ``pg_dump (PostgreSQL) 18.4``-style output.

    Raises ``ValueError`` for any unparseable output so callers fail closed.
    """
    match = _CLIENT_VERSION_PATTERN.search(output)
    if match is None:
        raise ValueError("unparseable client version output")
    return match.group(1)


def resolve_client_tool(tool_name: str) -> Path:
    """Resolve one PostgreSQL client binary, failing closed when missing."""
    resolved = shutil.which(tool_name)
    if resolved is None:
        raise _dependency_unavailable()
    return Path(resolved)


async def check_client_tools(
    dump_tool: str,
    restore_tool: str,
    *,
    runner: BoundedChildRunner = run_bounded_child,
) -> None:
    """Require both client binaries at the pinned exact version (spec 4.3).

    Must run before snapshot acquisition; a missing binary, a failed probe, a
    timeout or any older, newer or unparseable version is the same closed
    dependency failure.
    """
    for tool_name in (dump_tool, restore_tool):
        binary = resolve_client_tool(tool_name)
        try:
            result = await runner(
                [str(binary), "--version"],
                env=_sanitized_child_environment(),
                timeout_seconds=VERSION_PROBE_TIMEOUT_SECONDS,
            )
        except Exception:
            raise _dependency_unavailable() from None
        if result.timed_out or result.returncode != 0:
            raise _dependency_unavailable()
        try:
            version = parse_client_version(result.stdout)
        except ValueError:
            raise _dependency_unavailable() from None
        if version != EXPECTED_POSTGRESQL_CLIENT_VERSION:
            raise _dependency_unavailable()


def _sanitized_child_environment() -> dict[str, str]:
    """``os.environ`` minus every ``PG*`` key and ``DATABASE_URL``."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PG") and key != "DATABASE_URL"
    }


def _dependency_unavailable() -> RecoveryError:
    return RecoveryError(
        ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE,
        safe_details={"dependency": RecoveryDependency.PG_CLIENT},
    )


def _integrity_failed() -> RecoveryError:
    return RecoveryError(
        ErrorCode.CANONICAL_RECOVERY_INTEGRITY_FAILED,
        safe_details={"component": RecoveryComponent.POSTGRES_DUMP},
    )


def _restore_failed() -> RecoveryError:
    return RecoveryError(
        ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED,
        safe_details={"component": RecoveryComponent.POSTGRES_RESTORE},
    )


def _stream_dump_receipt(output_file: Path) -> DumpReceipt:
    """Hash the completed dump by streaming in 1 MiB chunks."""
    digest = hashlib.sha256()
    size_bytes = 0
    with output_file.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_SIZE_BYTES):
            digest.update(chunk)
            size_bytes += len(chunk)
    return DumpReceipt(size_bytes=size_bytes, sha256=digest.hexdigest())


class PostgresqlDumpProcessAdapter:
    """Bounded ``pg_dump``/``pg_restore`` boundary behind the recovery port."""

    def __init__(
        self,
        dump_binary: str | Path,
        restore_binary: str | Path,
        *,
        password: SecretStr,
        runner: BoundedChildRunner = run_bounded_child,
    ) -> None:
        self._dump_binary = dump_binary
        self._restore_binary = restore_binary
        self._password = password
        self._runner = runner

    async def create_dump(
        self,
        snapshot_token: str,
        output_file: Path,
        target: PostgresqlConnectionTarget,
        *,
        timeout_seconds: float = 600.0,
    ) -> DumpReceipt:
        """Dump one consistent snapshot into ``output_file`` (spec 9.3)."""
        argv = [
            str(self._dump_binary),
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--no-password",
            "--lock-wait-timeout=15000",
            f"--snapshot={snapshot_token}",
            f"--file={output_file}",
            "--host",
            target.host,
            "--port",
            str(target.port),
            "--username",
            target.user,
            target.database,
        ]
        async with self._ephemeral_passfile(target) as passfile_path:
            result = await self._run_bounded(
                argv, env=self._child_env(passfile_path), timeout_seconds=timeout_seconds
            )
        if result.timed_out or result.returncode != 0:
            raise _integrity_failed()
        try:
            return _stream_dump_receipt(output_file)
        except OSError:
            raise _integrity_failed() from None

    async def restore_dump(
        self,
        input_file: Path,
        target: PostgresqlConnectionTarget,
        *,
        timeout_seconds: float = 600.0,
    ) -> RestoreReceipt:
        """Restore one dump in a single transaction (spec 11.2)."""
        argv = [
            str(self._restore_binary),
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--no-password",
            "--host",
            target.host,
            "--port",
            str(target.port),
            "--username",
            target.user,
            "--dbname",
            target.database,
            str(input_file),
        ]
        async with self._ephemeral_passfile(target) as passfile_path:
            result = await self._run_bounded(
                argv, env=self._child_env(passfile_path), timeout_seconds=timeout_seconds
            )
        if result.timed_out or result.returncode != 0:
            raise _restore_failed()
        return RestoreReceipt(completed_at=datetime.now(UTC))

    async def _run_bounded(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> ProcessRunResult:
        try:
            return await self._runner(argv, env=env, timeout_seconds=timeout_seconds)
        except RecoveryError:
            raise
        except Exception:
            # The runner's failure detail (which may quote its inputs) never
            # survives this boundary; only the closed token does.
            raise _integrity_failed() from None

    def _child_env(self, passfile_path: Path) -> dict[str, str]:
        """Sanitized environment plus exactly ``PGPASSFILE``."""
        environment = _sanitized_child_environment()
        environment["PGPASSFILE"] = str(passfile_path)
        return environment

    @asynccontextmanager
    async def _ephemeral_passfile(self, target: PostgresqlConnectionTarget) -> AsyncIterator[Path]:
        """Ephemeral mode-0600 libpq password file outside the bundle root.

        Created in the system temp directory, removed in ``finally`` including
        on cancellation. The line format is
        ``host:port:database:user:password``.
        """
        file_descriptor, passfile_text = tempfile.mkstemp(
            prefix="knowledge-pgpass-", dir=tempfile.gettempdir()
        )
        passfile_path = Path(passfile_text)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(
                    f"{target.host}:{target.port}:{target.database}:"
                    f"{target.user}:{self._password.get_secret_value()}\n"
                )
            if os.name == "posix":
                os.chmod(passfile_path, _PASSFILE_MODE_POSIX)
            yield passfile_path
        finally:
            with suppress(OSError):
                passfile_path.unlink()
