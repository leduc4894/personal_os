"""Provider-neutral ports for the small-file sync orchestration (spec 10).

The seams task 6 (durable PostgreSQL adapter) and task 7
(:class:`~personal_os.small_file_sync.service.SmallFileSyncService`) build
on: the injectable aware UTC clock, the durable upload-operation store and
the server-side exclusion-policy guard, plus the Child 8
conflict-capture gateway through which a verified stale-update candidate is
retained as conflict evidence. The store port exposes no SQLAlchemy
row, database exception, R2 key, receipt or provider payload, and receives
the server-owned :class:`~personal_os.diagnostics.context.DiagnosticContext`
for correlation. Token minting, expiry durations and row locking are the
adapter's own concerns; only the domain values cross this boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.enforcement import AllowedPolicyRevisionBinding
from personal_os.object_storage import VerifiedObjectReceipt
from personal_os.small_file_sync.contracts import (
    BoundSmallFileOperation,
    SmallFileConflictCaptureResult,
    SmallFileDeviceContext,
    SmallFilePreflight,
    SmallFileTerminalResult,
    SmallFileUploadOperation,
    UploadOperationToken,
)
from personal_os.sources.commands import CreateSourceVersion, UpdateSourceVersion
from personal_os.sources.results import SourceVersionPublicationResult

#: Injectable clock returning the current aware UTC moment.
type AwareUtcClock = Callable[[], datetime]

#: The receive-side binding is the new :class:`BoundSmallFileOperation` —
#: it carries the bound initial locator evidence alongside every immutable
#: operation field. Older call sites that still reference
#: :class:`SmallFileBoundOperation` see the same shape through this alias.
SmallFileBoundOperation = BoundSmallFileOperation

__all__ = [
    "BoundSmallFileOperation",
    "SmallFileBoundOperation",
    "SmallFileConflictCaptureGateway",
]


class SmallFilePolicyGuard(Protocol):
    """Server-side re-evaluation of the active exclusion policy (spec 9).

    The composition root binds the domain
    :class:`~personal_os.exclusion_policy.enforcement.PolicyEnforcementService`
    here: the small-file subject carries the preflight's normalized locator
    evidence, which the publication-boundary guard never sees, so the adapter
    evaluates it through ``authorize_preflight`` at the single-part-upload
    boundary. A definite exclusion, an indeterminate outcome or any
    fail-closed policy failure raises the typed
    :class:`~personal_os.exclusion_policy.errors.ExclusionPolicyError`; only
    an allowed decision returns the server-owned revision binding. The guard
    runs before any operation-store reservation or object-store access.
    """

    async def authorize_small_file(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> AllowedPolicyRevisionBinding: ...


class SmallFilePublicationGateway(Protocol):
    """Publish one verified small-file operation with bound policy evidence.

    The receive orchestration reconstructs the immutable binding from its
    durable operation row and passes it explicitly. Implementations must not
    recover policy state from the plugin request or retain a current binding.
    """

    async def publish_create(
        self,
        *,
        command: CreateSourceVersion,
        stream: AsyncIterable[bytes],
        policy_binding: AllowedPolicyRevisionBinding,
        bound_operation: SmallFileBoundOperation,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult: ...

    async def publish_update(
        self,
        *,
        command: UpdateSourceVersion,
        stream: AsyncIterable[bytes],
        policy_binding: AllowedPolicyRevisionBinding,
        bound_operation: SmallFileBoundOperation,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult: ...


class SmallFileConflictCaptureGateway(Protocol):
    """Retain one verified stale-update candidate as conflict evidence.

    The Child 8 bridge seam: the composition root binds the shared
    :class:`~personal_os.source_conflicts.service.SourceConflictService`
    behind this port, so the small-file domain depends on a provider-neutral
    capability instead of the conflict domain module. ``capture_stale_update``
    receives the operation's frozen identity and the receipt of bytes that
    already cleared the bounded size/digest verification — verified-object
    admission stays on this boundary, never inside the conflict domain — and
    returns only the opaque capture receipt; the underlying capture replays
    by the event's idempotency identity, so a duplicate delivery returns the
    original conflict. ``resolve_captured_conflict`` is the replay lookup a
    same-identity preflight performs before its normal
    published/no-change classifier (Child 8 spec 5.1). No raw byte, digest,
    locator or object key crosses this boundary in either direction.
    """

    async def capture_stale_update(
        self,
        *,
        bound_operation: BoundSmallFileOperation,
        verified_candidate: VerifiedObjectReceipt,
        observed_remote_version_id: UUID | None,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileConflictCaptureResult: ...

    async def resolve_captured_conflict(
        self,
        *,
        workspace_id: UUID,
        originating_event_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileConflictCaptureResult | None: ...


class SmallFileUploadOperationStore(Protocol):
    """Durable upload-operation store port: replay, reservation, terminal write.

    ``resolve_terminal_result`` performs the exact-replay lookup by
    device/event/idempotency identity: a same-identity preflight after a lost
    commit response gets the frozen terminal result without allocating
    another operation, source or version. ``reserve_operation`` records the
    pending operation bound to one declared fingerprint; for a create it may
    reserve the internal UUID for the future publication but never inserts a
    ``sources`` row — canonical state is written only by the terminal writes
    after verified bytes commit. ``resolve_bound_operation`` binds one
    receive to the exact operation its opaque token names (closed
    not-found/identity-mismatch/expired/state-invalid failures included) and
    ``record_bound_terminal_result`` is the receive-side guarded terminal
    write over that binding. ``record_bound_terminal_failure`` is the same
    guarded write for a typed non-retryable business rejection: it lands the
    terminal ``failed`` state with the closed registry token only, so a
    typed rejection never leaves the claimed row fenced in ``receiving``.
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
        policy_binding: AllowedPolicyRevisionBinding,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileUploadOperation: ...

    async def record_terminal_result(
        self,
        operation: SmallFileUploadOperation,
        result: SmallFileTerminalResult,
        diagnostic_context: DiagnosticContext,
    ) -> None: ...

    async def resolve_bound_operation(
        self,
        operation_token: UploadOperationToken,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileBoundOperation: ...

    async def record_bound_terminal_result(
        self,
        bound: SmallFileBoundOperation,
        result: SmallFileTerminalResult,
        diagnostic_context: DiagnosticContext,
    ) -> None: ...

    async def record_bound_terminal_failure(
        self,
        bound: SmallFileBoundOperation,
        error_code: ErrorCode,
        diagnostic_context: DiagnosticContext,
    ) -> None: ...
