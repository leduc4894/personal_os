"""Verified exact-version content reading for device downloads (spec 7.4).

:class:`VerifiedDeviceContentService` composes the PostgreSQL device content
catalog with the fully verified spool-backed object reader: the descriptor
resolves membership and current-policy authorization first, then the
existing ``open_verified_reader`` performs the exact HEAD plus conditional
full GET verification, and the consumer's iterator is entered only after
digest, size and media verification completed — exact bytes flow from a
verified spool, never straight from a provider.

Every failure crosses the boundary through a closed code with the Task 1
diagnostic reason recorded through the ``download`` operation vocabulary and
the three registered device sync events: a device sync rejection emits the
closed reason before the typed error re-raises unchanged, an object absence,
corruption, metadata conflict or broken provider contract maps onto the
closed device download integrity failure, and every other object failure is
the retryable dependency outage. The authorization boundary's own closed
rejection (the registry's policy denial) and caller cancellation pass
through unmetered, and no identifier, locator, digest, object key or
provider detail ever becomes a metric label or event field. The module is
transport-agnostic: it imports no web framework, registers no route and
depends only on the descriptor seam and the provider-neutral object-storage
port.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID

from personal_os.device_sync.contracts import (
    DeviceContentDescriptor,
    DeviceSyncContext,
)
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.device_sync.metrics import (
    DeviceSyncMetrics,
    DeviceSyncOperation,
    DeviceSyncOutcome,
)
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import (
    DiagnosticEventSink,
    EventName,
    RejectedDiagnosticPayload,
    build_registered_event,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.object_storage import ExpectedObject, VerifiedObjectReader
from personal_os.object_storage.errors import ObjectStorageError

_MILLISECONDS_PER_SECOND: Final[float] = 1_000.0

#: The object-storage codes that are the download's own integrity failure:
#: ordinary absence under a canonical key, digest/size corruption, a stored
#: metadata conflict and a broken provider contract. Every other code is an
#: availability, capacity or configuration dependency outage.
_DOWNLOAD_INTEGRITY_OBJECT_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.OBJECT_STORAGE_OBJECT_MISSING,
        ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED,
        ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT,
        ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID,
    }
)


class DeviceContentCatalog(Protocol):
    """The exact-version descriptor seam the verified download composes over.

    Satisfied structurally by
    :class:`~postgresql_source_store.device_content_catalog.PostgresqlDeviceContentCatalog`:
    membership, workspace scope and current-policy authorization resolve
    here, before any byte is fetched.
    """

    async def resolve_descriptor(
        self,
        context: DeviceSyncContext,
        *,
        source_id: UUID,
        source_version_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceContentDescriptor: ...


class VerifiedObjectSource(Protocol):
    """The verified reader seam the download composes over.

    Satisfied structurally by the provider-neutral
    :class:`~personal_os.object_storage.contracts.CanonicalObjectStore`: the
    reader context is entered only after the adapter independently verified
    the exact size, media type and full digest of the expected bytes.
    """

    def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AbstractAsyncContextManager[VerifiedObjectReader]: ...


@dataclass(frozen=True, slots=True)
class VerifiedDeviceContent:
    """One exact verified version: its descriptor and verified reader.

    Carries only the expected digest/size/media descriptor and the reader
    over the already-verified bytes — never an object key, presigned URL,
    receipt or provider detail.
    """

    descriptor: DeviceContentDescriptor
    reader: VerifiedObjectReader

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"


def map_object_storage_failure(cause: ObjectStorageError) -> DeviceSyncError:
    """Map one reader-path object failure onto the closed device boundary.

    The four fail-closed verification signals — ordinary absence under a
    canonical key, digest/size corruption, a stored metadata conflict and a
    broken provider contract — are the download's own non-retryable
    integrity failure; every other object-storage code is an availability,
    capacity or configuration dependency outage the caller may retry. The
    provider cause remains chained only: its text, keys and endpoints never
    enter the typed error.
    """

    if cause.error_code in _DOWNLOAD_INTEGRITY_OBJECT_CODES:
        return DeviceSyncError(DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED)
    return DeviceSyncError(DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE)


class VerifiedDeviceContentService:
    """Instrumented verified exact-version content reading for downloads.

    Depends only on the descriptor seam, the verified reader seam, the
    closed metrics sink, the optional structured-event sink and the
    injectable monotonic clock. ``open_content`` resolves the descriptor
    (membership and current policy, before any byte) and then enters the
    fully verified reader; the consumer receives a
    :class:`VerifiedDeviceContent` only after full verification completed.
    """

    def __init__(
        self,
        *,
        catalog: DeviceContentCatalog,
        objects: VerifiedObjectSource,
        metrics: DeviceSyncMetrics,
        diagnostics: DiagnosticEventSink | None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._catalog = catalog
        self._objects = objects
        self._metrics = metrics
        self._diagnostics = diagnostics
        self._monotonic = monotonic

    def open_content(
        self,
        context: DeviceSyncContext,
        *,
        source_id: UUID,
        source_version_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> AbstractAsyncContextManager[VerifiedDeviceContent]:
        """Open one exact verified version for reading.

        Errors detected before the content context is entered — membership,
        policy, integrity — raise the closed typed error with the diagnostic
        reason recorded; once the consumer holds the verified reader, a
        consumer-side failure is its own and is never metered as a device
        operation outcome.
        """

        return self._open_content(
            context,
            source_id=source_id,
            source_version_id=source_version_id,
            diagnostic_context=diagnostic_context,
        )

    @asynccontextmanager
    async def _open_content(
        self,
        context: DeviceSyncContext,
        *,
        source_id: UUID,
        source_version_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> AsyncIterator[VerifiedDeviceContent]:
        started = self._monotonic()
        try:
            descriptor = await self._catalog.resolve_descriptor(
                context,
                source_id=source_id,
                source_version_id=source_version_id,
                diagnostic_context=diagnostic_context,
            )
        except DeviceSyncError as error:
            self._record_failure(error, started)
            raise
        reader_lifetime = AsyncExitStack()
        try:
            try:
                # The verified reader context is entered before the consumer
                # receives anything: exact bytes exist only after the full
                # verification the adapter performs on entry.
                reader = await reader_lifetime.enter_async_context(
                    self._objects.open_verified_reader(descriptor.expected_object())
                )
            except ObjectStorageError as cause:
                mapped = map_object_storage_failure(cause)
                self._record_failure(mapped, started)
                raise mapped from cause
            yield VerifiedDeviceContent(descriptor=descriptor, reader=reader)
            self._record_success(started)
        finally:
            await reader_lifetime.aclose()

    def _record_success(self, started: float) -> None:
        duration_ms = self._elapsed_ms_since(started)
        self._metrics.record_operation(
            operation=DeviceSyncOperation.DOWNLOAD,
            outcome=DeviceSyncOutcome.SUCCEEDED,
            reason=None,
            duration_ms=duration_ms,
        )
        self._emit_registered_event(
            EventName.DEVICE_SYNC_OPERATION_COMPLETED,
            {"operation": DeviceSyncOperation.DOWNLOAD, "duration_ms": duration_ms},
        )

    def _record_failure(self, error: DeviceSyncError, started: float) -> None:
        duration_ms = self._elapsed_ms_since(started)
        outcome = DeviceSyncOutcome.REJECTED if not error.is_retryable else DeviceSyncOutcome.FAILED
        event_name = (
            EventName.DEVICE_SYNC_OPERATION_REJECTED
            if outcome is DeviceSyncOutcome.REJECTED
            else EventName.DEVICE_SYNC_OPERATION_FAILED
        )
        self._metrics.record_operation(
            operation=DeviceSyncOperation.DOWNLOAD,
            outcome=outcome,
            reason=error.code,
            duration_ms=duration_ms,
        )
        self._emit_registered_event(
            event_name,
            {
                "operation": DeviceSyncOperation.DOWNLOAD,
                "reason": error.code,
                "duration_ms": duration_ms,
            },
        )

    def _emit_registered_event(self, event_name: EventName, fields: Mapping[str, object]) -> None:
        """Validate the registered event; deliver it when a sink is bound.

        Without a composition-provided sink the validated payload is
        discarded (build-and-validate only); a rejected payload is registry
        drift, a programming error rather than untrusted input, and raises
        regardless of sink presence.
        """

        built = build_registered_event(event_name, fields)
        if isinstance(built, RejectedDiagnosticPayload):
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        if self._diagnostics is not None:
            self._diagnostics.emit(event_name, dict(fields))

    def _elapsed_ms_since(self, started: float) -> int:
        # Clamped at zero so a clock seam that repeats or drifts backwards can
        # never turn a recorded duration negative.
        return max(int((self._monotonic() - started) * _MILLISECONDS_PER_SECOND), 0)


__all__ = [
    "DeviceContentCatalog",
    "VerifiedDeviceContent",
    "VerifiedDeviceContentService",
    "VerifiedObjectSource",
    "map_object_storage_failure",
]
