"""One-shot read-only R2 ``HeadBucket`` runtime diagnostic command.

This is the only place in the system that performs ``HeadBucket``. Startup
validation stays offline, liveness never calls R2 and normal readiness never
calls R2; the explicit ``object-storage-check-runtime`` command is the single
operator probe, and it is strictly read-only — it never puts, gets, lists or
deletes an object.

The module keeps the console-entry surface import-light: only the standard
library is imported at module top level, so importing the entry point and
resolving ``--help``/syntax errors never reads the environment, secret files
or the network. Every configuration, diagnostics and provider import happens
lazily inside the check itself, after the ``--service`` syntax decision.

The command sequence is exact:

.. code-block:: text

    create/bind correlation context
    -> load runtime + object-storage settings
    -> configure safe diagnostics
    -> run bounded read-only HeadBucket
    -> emit succeeded/degraded typed event
    -> close client

Exit codes are stable: ``0`` success, ``2`` CLI syntax (decided before any
environment or secret read), ``69`` dependency/access failure after a bounded
retry, ``70`` unexpected internal failure, ``78`` configuration or secret
failure. Settings values, secret paths and exception causes are never rendered;
the only output is one safe JSON diagnostic line plus the exit code.

The bounded retry around the probe lives here, not in
:meth:`r2_object_storage.error_mapping.RetryPolicy.run`: the client's
``head_bucket`` raises typed :class:`ObjectStorageError` values that
``classify_r2_failure`` does not recognize, so wrapping the probe in
``RetryPolicy.run`` would re-map a typed error to ``internal_error``. This
loop instead decides typed errors by their error code (``unavailable`` retries,
``access_denied`` is terminal) and classifies only raw provider exceptions.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn, Protocol

if TYPE_CHECKING:
    from personal_os.object_storage.errors import ObjectStorageError
    from personal_os.runtime_configuration.models import ServiceName
    from r2_object_storage.client import S3ClientProtocol
    from r2_object_storage.settings import LoadedR2Credentials, ObjectStorageSettings
    from r2_object_storage.spool import SpoolCleanupSummary

__all__ = [
    "EX_CONFIG",
    "EX_OK",
    "EX_SOFTWARE",
    "EX_SYNTAX",
    "EX_UNAVAILABLE",
    "run",
    "run_object_storage_runtime_check",
]

#: Success: the read-only probe completed.
EX_OK: Final[int] = 0
#: CLI syntax error, decided before any environment or secret read.
EX_SYNTAX: Final[int] = 2
#: Dependency/access failure: unavailable after the bounded retry or denied.
EX_UNAVAILABLE: Final[int] = 69
#: Unexpected internal failure.
EX_SOFTWARE: Final[int] = 70
#: Configuration or secret failure.
EX_CONFIG: Final[int] = 78

#: The bounded retry around the one-shot probe mirrors the adapter's
#: ``RetryPolicy.maximum_attempts`` default of three.
_MAXIMUM_PROBE_ATTEMPTS: Final[int] = 3
_PROBE_RETRY_BASE_DELAY_SECONDS: Final[float] = 0.5
_PROBE_OPERATION_TOKEN: Final[str] = "head_bucket"
_JANITOR_OPERATION_TOKEN: Final[str] = "spool_cleanup"
_JANITOR_FAILED_REASON_TOKEN: Final[str] = "spool_cleanup_janitor_failed"
_CLIENT_CLOSE_OPERATION_TOKEN: Final[str] = "object_storage_client_close"
_CLIENT_CLOSE_FAILED_REASON_TOKEN: Final[str] = "object_storage_client_close_failed"
_PROVIDER_TOKEN: Final[str] = "r2"


class R2ClientSource(Protocol):
    """The client-manager surface the runtime check depends on."""

    async def get_client(self) -> S3ClientProtocol: ...

    async def close(self) -> None: ...


class R2ClientSourceFactory(Protocol):
    """Structural factory building one client source from loaded settings."""

    def __call__(
        self,
        settings: ObjectStorageSettings,
        credentials: LoadedR2Credentials,
    ) -> R2ClientSource: ...


type SpoolJanitor = Callable[[Path], Awaitable[SpoolCleanupSummary]]


@dataclass(frozen=True, slots=True)
class HeadBucketProbeOutcome:
    """The bounded probe result: attempts, duration and optional typed failure."""

    attempt_count: int
    duration_ms: int
    failure: ObjectStorageError | None


def _default_client_source(
    settings: ObjectStorageSettings,
    credentials: LoadedR2Credentials,
) -> R2ClientSource:
    from r2_object_storage.client import R2ClientManager

    return R2ClientManager(settings, credentials)


async def _default_spool_janitor(spool_root: Path) -> SpoolCleanupSummary:
    from r2_object_storage.spool import SpoolManager

    return await SpoolManager(spool_root).cleanup_stale_spools()


def _duration_ms(started: float, monotonic: Callable[[], float]) -> int:
    return max(0, int((monotonic() - started) * 1000))


def _retry_delay_seconds(attempt_count: int) -> float:
    delay_seconds: float = _PROBE_RETRY_BASE_DELAY_SECONDS * float(2 ** (attempt_count - 1))
    return delay_seconds


async def _run_bounded_head_bucket(
    client: S3ClientProtocol,
    *,
    started: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
) -> HeadBucketProbeOutcome:
    """Run the read-only ``HeadBucket`` probe behind a command-level bounded retry.

    Typed :class:`ObjectStorageError` outcomes are decided by their error code:
    ``object_storage_unavailable`` retries up to the attempt bound, every other
    code (``access_denied`` in particular) is terminal. Raw provider exceptions
    are classified with :func:`classify_r2_failure` and, when the decision is
    terminal or the bound is exhausted, mapped with :func:`map_r2_failure` —
    access denial becomes ``object_storage_access_denied``. An unknown
    exception is mapped to ``internal_error`` (spec §12), escapes this helper
    and is handled by the command's unexpected-failure path (exit ``70``). The
    probe is never wrapped in ``RetryPolicy.run`` because that would re-map a
    typed ``ObjectStorageError`` to ``internal_error``.
    """

    from personal_os.error_contracts.codes import ErrorCode
    from personal_os.object_storage.errors import ObjectStorageError
    from r2_object_storage.error_mapping import RetryDecision, classify_r2_failure, map_r2_failure

    attempt_count = 0
    while True:
        attempt_count += 1
        try:
            await client.head_bucket()
            return HeadBucketProbeOutcome(
                attempt_count=attempt_count,
                duration_ms=_duration_ms(started, monotonic),
                failure=None,
            )
        except asyncio.CancelledError:
            raise
        except ObjectStorageError as error:
            is_retryable_unavailable = (
                error.error_code is ErrorCode.OBJECT_STORAGE_UNAVAILABLE
                and attempt_count < _MAXIMUM_PROBE_ATTEMPTS
            )
            if not is_retryable_unavailable:
                return HeadBucketProbeOutcome(
                    attempt_count=attempt_count,
                    duration_ms=_duration_ms(started, monotonic),
                    failure=error,
                )
        except Exception as cause:
            decision = classify_r2_failure(cause)
            should_retry = (
                decision is RetryDecision.RETRY and attempt_count < _MAXIMUM_PROBE_ATTEMPTS
            )
            if not should_retry:
                try:
                    raise map_r2_failure(cause, exhausted=False) from cause
                except ObjectStorageError as mapped:
                    return HeadBucketProbeOutcome(
                        attempt_count=attempt_count,
                        duration_ms=_duration_ms(started, monotonic),
                        failure=mapped,
                    )
        await sleep(_retry_delay_seconds(attempt_count))


async def run_object_storage_runtime_check(
    service: ServiceName,
    *,
    environ: Mapping[str, str] | None = None,
    client_source_factory: R2ClientSourceFactory = _default_client_source,
    spool_janitor: SpoolJanitor = _default_spool_janitor,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Run the exact one-shot read-only runtime-check sequence for one service.

    Every path closes the client source exactly once and the exit code always
    reflects the probe outcome. The clean-success path emits exactly one safe
    JSON event; a degraded startup janitor adds one warning event with safe
    counts (never skipping the probe and never changing the exit code);
    configuration failures exit ``78`` before any client exists; a probe
    dependency/access failure exits ``69``; an unexpected internal failure
    exits ``70``. No settings value, secret path or exception cause is ever
    rendered.
    """

    from personal_os.diagnostics.context import bind_diagnostic_context, create_diagnostic_context
    from personal_os.diagnostics.events import EventName, SafeToken
    from personal_os.diagnostics.logging import (
        configure_diagnostics,
        emit_emergency_application_error,
        emit_emergency_internal_error,
    )
    from personal_os.error_contracts.codes import ErrorCategory, ErrorCode
    from personal_os.error_contracts.exceptions import ApplicationError
    from personal_os.object_storage.errors import ObjectStorageError
    from personal_os.runtime_configuration.loading import load_runtime_settings
    from r2_object_storage.settings import load_object_storage_settings

    provider = SafeToken.parse(_PROVIDER_TOKEN)
    probe_operation = SafeToken.parse(_PROBE_OPERATION_TOKEN)
    janitor_operation = SafeToken.parse(_JANITOR_OPERATION_TOKEN)
    janitor_failed_reason = SafeToken.parse(_JANITOR_FAILED_REASON_TOKEN)
    client_close_operation = SafeToken.parse(_CLIENT_CLOSE_OPERATION_TOKEN)
    client_close_failed_reason = SafeToken.parse(_CLIENT_CLOSE_FAILED_REASON_TOKEN)

    resolution = create_diagnostic_context()
    context = resolution.context
    with bind_diagnostic_context(context):
        try:
            runtime_settings = load_runtime_settings(service_name=service, environ=environ)
            object_settings, credentials = load_object_storage_settings(environ=environ)
        except ApplicationError as error:
            emit_emergency_application_error(service, context, error)
            return EX_CONFIG
        except Exception as error:
            emit_emergency_internal_error(service, context, error)
            return EX_SOFTWARE

        try:
            logger = configure_diagnostics(runtime_settings)
        except Exception as error:
            emit_emergency_internal_error(service, context, error)
            return EX_SOFTWARE

        client_source = client_source_factory(object_settings, credentials)
        try:
            # The janitor always runs first. A degraded janitor is a warning
            # (spec §9.3: handled by a later run), so it emits one safe-counts
            # event and execution continues to the probe — a local spool
            # problem never disables the sole read-only HeadBucket diagnostic
            # and never changes the exit code (spec §14.2). Spec §9.3 also
            # pins the deferred count: candidates beyond the per-run bound
            # emit the degraded event with the real remaining count and are
            # handled by a later run, so a successful summary with deferred
            # candidates still emits the warning while the probe continues.
            deferred_count = 0
            cleanup_reason: SafeToken | None = None
            try:
                summary = await spool_janitor(object_settings.object_storage_spool_root)
                deferred_count = summary.deferred_count
                cleanup_reason = summary.reason
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed janitor is degraded with an unknown deferred count;
                # no summary is available on the failure path, so count is 0.
                cleanup_reason = janitor_failed_reason
            if cleanup_reason is not None:
                logger.emit(
                    EventName.OBJECT_STORAGE_SPOOL_CLEANUP_DEGRADED,
                    {
                        "operation": janitor_operation,
                        "count": deferred_count,
                        "reason": cleanup_reason,
                    },
                )

            started: float | None = None
            try:
                client = await client_source.get_client()
                started = monotonic()
                outcome = await _run_bounded_head_bucket(
                    client, started=started, monotonic=monotonic, sleep=sleep
                )
            except asyncio.CancelledError:
                raise
            except ObjectStorageError as error:
                outcome = HeadBucketProbeOutcome(
                    attempt_count=1,
                    duration_ms=0 if started is None else _duration_ms(started, monotonic),
                    failure=error,
                )
            except Exception as error:
                logger.emit_internal_error(error)
                return EX_SOFTWARE

            failure = outcome.failure
            if failure is None:
                logger.emit(
                    EventName.OBJECT_STORAGE_OPERATION_SUCCEEDED,
                    {
                        "operation": probe_operation,
                        "duration_ms": outcome.duration_ms,
                        "size_bytes": 0,
                        "attempt_count": outcome.attempt_count,
                        "provider": provider,
                    },
                )
                return EX_OK
            logger.emit(
                EventName.OBJECT_STORAGE_OPERATION_FAILED,
                {
                    "operation": probe_operation,
                    "duration_ms": outcome.duration_ms,
                    "attempt_count": outcome.attempt_count,
                    "provider": provider,
                    "error_code": failure.error_code,
                    "error_category": failure.category,
                    "is_retryable": failure.is_retryable,
                },
            )
            return EX_UNAVAILABLE
        finally:
            # The client manager's close is idempotent; it runs exactly once on
            # every completed path. A close failure never replaces the probe's
            # already-determined exit code, but remains observable with only
            # fixed closed fields; cancellation always propagates.
            try:
                await client_source.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.emit(
                    EventName.OBJECT_STORAGE_CLIENT_CLOSE_DEGRADED,
                    {
                        "operation": client_close_operation,
                        "reason": client_close_failed_reason,
                        "error_code": ErrorCode.INTERNAL_ERROR,
                        "error_category": ErrorCategory.INTERNAL,
                        "is_retryable": False,
                    },
                )


def _build_argument_parser() -> argparse.ArgumentParser:
    from personal_os.runtime_configuration.models import ServiceName

    parser = argparse.ArgumentParser(
        prog="object-storage-check-runtime",
        description=(
            "one-shot read-only R2 HeadBucket diagnostic; emits one safe JSON "
            "diagnostic line and exits 0, 2, 69, 70 or 78"
        ),
    )
    parser.add_argument(
        "--service",
        required=True,
        choices=tuple(member.value for member in ServiceName),
        help="the composition root whose settings are loaded and probed",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """Parse ``--service`` first, then run the check and return the exit code.

    The syntax decision happens before any environment or secret file is read:
    a missing or unknown ``--service`` value exits ``2`` and ``--help`` exits
    ``0`` without touching configuration, secrets or the network.
    """

    from personal_os.runtime_configuration.models import ServiceName

    parser = _build_argument_parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exit_request:
        code = exit_request.code
        return code if isinstance(code, int) else EX_SYNTAX
    service = ServiceName(arguments.service)
    return asyncio.run(run_object_storage_runtime_check(service))


def main() -> NoReturn:
    raise SystemExit(run())
