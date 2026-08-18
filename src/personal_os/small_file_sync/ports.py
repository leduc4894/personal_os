"""Provider-neutral ports for the small-file sync orchestration (spec 10).

The seams task 6 (durable PostgreSQL adapter) and task 7
(:class:`~personal_os.small_file_sync.service.SmallFileSyncService`) build
on: the injectable aware UTC clock and the durable upload-operation store.
The port exposes no SQLAlchemy row, database exception, R2 key, receipt or
provider payload, and receives the server-owned
:class:`~personal_os.diagnostics.context.DiagnosticContext` for correlation.
Token minting, expiry durations and row locking are the adapter's own
concerns; only the domain values cross this boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.small_file_sync.contracts import (
    SmallFileDeviceContext,
    SmallFilePreflight,
    SmallFileTerminalResult,
    SmallFileUploadOperation,
)

#: Injectable clock returning the current aware UTC moment.
type AwareUtcClock = Callable[[], datetime]


class SmallFileUploadOperationStore(Protocol):
    """Durable upload-operation store port: replay, reservation, terminal write.

    ``resolve_terminal_result`` performs the exact-replay lookup by
    device/event/idempotency identity: a same-identity preflight after a lost
    commit response gets the frozen terminal result without allocating
    another operation, source or version. ``reserve_operation`` records the
    pending operation bound to one declared fingerprint; for a create it may
    reserve the internal UUID for the future publication but never inserts a
    ``sources`` row — canonical state is written only by
    ``record_terminal_result`` after verified bytes commit.
    """

    async def resolve_terminal_result(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileTerminalResult | None: ...

    async def reserve_operation(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileUploadOperation: ...

    async def record_terminal_result(
        self,
        operation: SmallFileUploadOperation,
        result: SmallFileTerminalResult,
        diagnostic_context: DiagnosticContext,
    ) -> None: ...
