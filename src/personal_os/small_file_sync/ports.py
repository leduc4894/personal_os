"""Provider-neutral ports for the small-file sync orchestration (spec 10).

The seams task 6 (durable PostgreSQL adapter) and task 7
(:class:`~personal_os.small_file_sync.service.SmallFileSyncService`) build
on: the injectable aware UTC clock, the durable upload-operation store and
the server-side exclusion-policy guard. The store port exposes no SQLAlchemy
row, database exception, R2 key, receipt or provider payload, and receives
the server-owned :class:`~personal_os.diagnostics.context.DiagnosticContext`
for correlation. Token minting, expiry durations and row locking are the
adapter's own concerns; only the domain values cross this boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.exclusion_policy.enforcement import AllowedPolicyRevisionBinding
from personal_os.object_storage import CanonicalMediaType, ContentDigest
from personal_os.small_file_sync.contracts import (
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFileTerminalResult,
    SmallFileUploadOperation,
    UploadOperationToken,
)

#: Injectable clock returning the current aware UTC moment.
type AwareUtcClock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class SmallFileBoundOperation:
    """The durable receive-side view of one upload operation, token-bound.

    Reconstructed by the store from the operation row alone: the
    credential-derived identity, the declared fingerprint fields the row
    froze at reservation, the create's reserved canonical UUID (or the
    update's source/base pair), the expiry deadline and — for an
    already-committed operation — the frozen terminal result an exact replay
    returns unchanged. The normalized locator and local file id are
    deliberately absent: the row never stores a path, so the value carries
    exactly what the content stream needs and permits no payload
    substitution, no locator echo and no receipt or object-store detail.
    """

    operation_token: UploadOperationToken
    workspace_id: UUID
    device_id: UUID
    event_id: UUID
    idempotency_key: SmallFileIdempotencyKey
    operation: SmallFileOperation
    declared_sha256: ContentDigest
    declared_size_bytes: int
    declared_media_type: CanonicalMediaType
    policy_revision_number: int
    reserved_source_id: UUID | None
    update_source_id: UUID | None
    update_base_version_id: UUID | None
    expires_at: datetime
    terminal_result: SmallFileTerminalResult | None


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
    write over that binding.
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
