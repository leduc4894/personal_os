"""Composition of the multipart upload runtime: the serve seam and its offline double.

:func:`compose_offline_multipart_upload` builds the deterministic offline
graph the OpenAPI export and the route tests drive: an identity-keyed
in-memory session store mirroring the durable adapter semantics
(persist-before-create reservations, the fenced idempotent provider-identity
write, owner/expiry closure, geometry-windowed part facts, a finite
completion lease with the committed-replay shape), an evidence store binding
each session to its frozen bound operation, an operation store minting the
reserved create identity, a staging provider over the same validated
``staging/multipart/`` str-key seam the R2 adapter owns, a byte source
serving the seeded staging preimage through the real verification spool, a
publication gateway with an exactly-once commit counter, and the real
:class:`~personal_os.multipart_upload.service.MultipartUploadService` over
the in-memory low-cardinality metrics sink whose rejection ring is the
readable reason surface of the committed session's inline staging-delete
failure. It reads no environment value, no secret file, no database and no
provider client.

Two seams of this module resolve the parked review findings of the earlier
multipart tasks and stay load-bearing for the serve graph:

- :func:`multipart_recheck_locator_stand_in` derives — from the service's
  public reconstruction function itself, never a duplicated literal — the
  fixed locator a frozen UPDATE recheck carries because the durable bound
  operation drops update locators. The offline policy guard and the
  serve-reusable :class:`RecheckLocatorAwarePolicyEnforcementGuard`
  evaluate that recheck subject locator-free, so a locator-keyed rule
  observes indeterminate evidence and fails closed (parked finding D1): an
  advance to deny can never pass an early recheck through the stand-in
  locator as if it were real locator evidence.
- :data:`MultipartUploadRuntime.rejection_diagnostics` binds the metrics
  sink's ring read side into the composition's public surface — the same
  durable-trail pattern the small-file sync runtime serves its rejection
  ring through (parked finding D2) — so the committed session's inline
  exact staging-delete failure surfaces its closed reason token on a
  readable surface instead of being swallowed.

The serve graph binding (the durable PostgreSQL session store, the R2
staging provider behind the shared lazy client manager, the durable
evidence store and the exact staging-read capability) lands with the server
composition wiring of the next task: this module owns the runtime contract
that binding must satisfy — one shared R2 client per process, disposed
exactly once through the runtime's ``aclose`` hook, mirroring how the
small-file sync runtime closes its own client — together with the two
reusable adapters (the locator-aware recheck guard and the
:class:`ValidatedStagingKeyMultipartProvider` str-key seam) that binding
composes.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol
from uuid import UUID, uuid4

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.exclusion_policy.enforcement import AllowedPolicyRevisionBinding
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.metrics import PolicyBoundary
from personal_os.multipart_upload.contracts import (
    MultipartPartRange,
    MultipartPartUrl,
    MultipartSessionState,
    MultipartUploadSessionId,
    compute_multipart_session_expiry,
)
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.metrics import (
    InMemoryMultipartUploadMetrics,
    MultipartRejectionDiagnostics,
    MultipartRejectionDiagnosticsSource,
    MultipartRejectionReason,
    MultipartUploadMetricsWithRejectionDiagnostics,
)
from personal_os.multipart_upload.ports import (
    MultipartCleanupClaim,
    MultipartProviderPartETag,
    MultipartProviderUploadId,
    MultipartSessionClaim,
    MultipartSessionRecord,
)
from personal_os.multipart_upload.service import (
    MultipartObservedPart,
    MultipartUploadService,
    reconstruct_recheck_preflight,
)
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReader,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from personal_os.object_storage.errors import (
    DIGEST_MISMATCH,
    SIZE_MISMATCH,
    ObjectStorageError,
)
from personal_os.small_file_sync.contracts import (
    BoundSmallFileOperation,
    NormalizedLocator,
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFileTerminalResult,
    SmallFileUploadOperation,
    UploadOperationToken,
    compute_locator_fingerprint,
)
from personal_os.small_file_sync.errors import SmallFileSyncError
from personal_os.small_file_sync.ports import AwareUtcClock
from personal_os.sources.commands import (
    CreateSourceVersion,
    UpdateSourceVersion,
)
from personal_os.sources.reading import (
    CanonicalReadStateError,
    CanonicalSourceReference,
    ReadCurrentSourceCommand,
)
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult
from r2_object_storage.multipart import MultipartProviderPart, MultipartStagingKey

#: The ordinary part geometry constant the offline rows derive (8 MiB).
_OFFLINE_PART_SIZE_BYTES: Final[int] = 8 * 1024 * 1024

#: The finite completion lease of the offline store, mirroring the durable
#: adapter's ten-minute lease.
_OFFLINE_COMPLETION_LEASE_SECONDS: Final[int] = 600

#: Deterministic offline staging URL host: a non-routable reserved TLD so
#: no offline part-URL value can ever address a real provider endpoint.
_OFFLINE_STAGING_URL_PREFIX: Final[str] = "https://multipart-staging.invalid/"

#: Bounded chunk size of the offline staging byte source.
_OFFLINE_STAGING_CHUNK_BYTES: Final[int] = 1024 * 1024

_FORWARD_SESSION_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CREATED,
        MultipartSessionState.UPLOADING,
        MultipartSessionState.COMPLETING,
        MultipartSessionState.VERIFYING,
        MultipartSessionState.PROMOTING,
    }
)
_COMPLETION_FAMILY_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.COMPLETING,
        MultipartSessionState.VERIFYING,
        MultipartSessionState.PROMOTING,
    }
)
_PART_RECORDING_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {MultipartSessionState.CREATED, MultipartSessionState.UPLOADING}
)
_TERMINAL_OBLIGATION_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CANCELLING,
        MultipartSessionState.EXPIRED,
        MultipartSessionState.INTEGRITY_FAILED,
        MultipartSessionState.POLICY_DENIED,
        MultipartSessionState.CLEANUP_PENDING,
        MultipartSessionState.CLEANED,
    }
)


@dataclass(frozen=True, slots=True)
class MultipartUploadRuntime:
    """One composed multipart upload runtime the session routes consume.

    ``rejection_diagnostics`` exposes the metrics sink's read side — the
    closed rejection counters and the bounded ring, the readable reason
    surface of the committed session's inline staging-delete failure — for
    the diagnostics surface of the serve graph. ``aclose`` is the serve
    graph's disposal hook: the serve composition closes its one shared R2
    client exactly once through it; the offline graph owns no resource and
    leaves it unset.
    """

    service: MultipartUploadService
    rejection_diagnostics: MultipartRejectionDiagnosticsSource
    aclose: Callable[[], Awaitable[None]] | None = None


# --- the frozen-recheck locator seam (parked finding D1) -----------------------


def multipart_recheck_locator_stand_in() -> NormalizedLocator:
    """Derive the fixed locator a frozen UPDATE recheck carries.

    The durable bound operation deliberately drops an update's normalized
    locator, so the service's reconstruction function substitutes one fixed
    stand-in value. This seam derives that value by probing the public
    reconstruction function itself — no duplicated literal can drift from
    the domain — so a policy guard can recognize exactly the recheck shape
    and evaluate it locator-free instead of letting a locator-keyed rule
    match fabricated evidence.
    """

    probe = BoundSmallFileOperation(
        operation_id=uuid4(),
        operation_token=UploadOperationToken(secrets.token_urlsafe(32)),
        workspace_id=uuid4(),
        device_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=SmallFileIdempotencyKey(str(uuid4())),
        operation=SmallFileOperation.UPDATE,
        declared_sha256=ContentDigest.parse("0" * 64),
        declared_size_bytes=_OFFLINE_PART_SIZE_BYTES * 3,
        declared_media_type=CanonicalMediaType.parse("text/markdown"),
        policy_revision_number=1,
        reserved_source_id=None,
        update_source_id=uuid4(),
        update_base_version_id=uuid4(),
        normalized_locator=None,
        locator_fingerprint=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        terminal_result=None,
    )
    locator = reconstruct_recheck_preflight(probe).normalized_locator
    if locator is None:  # pragma: no cover - the reconstruction contract
        raise MultipartUploadError(ErrorCode.MULTIPART_SESSION_STATE_INVALID)
    return locator


def _is_locator_free_recheck(preflight: SmallFilePreflight, stand_in: NormalizedLocator) -> bool:
    """Report whether one preflight is the frozen UPDATE recheck shape."""

    return (
        preflight.operation is SmallFileOperation.UPDATE
        and preflight.normalized_locator is not None
        and preflight.normalized_locator.value == stand_in.value
    )


class PolicyPreflightEnforcement(Protocol):
    """The enforcement seam the locator-aware recheck guard evaluates."""

    async def authorize_preflight(
        self,
        *,
        subject: PolicySubject,
        boundary: PolicyBoundary,
        context: DiagnosticContext,
    ) -> AllowedPolicyRevisionBinding: ...


class RecheckLocatorAwarePolicyEnforcementGuard:
    """Serve-reusable policy guard failing the update recheck locator-free.

    A genuine create-time preflight evaluates on its full evidence — the
    real locator the wire body carried — while the frozen UPDATE recheck
    (recognized through :func:`multipart_recheck_locator_stand_in`, never a
    duplicated literal) evaluates on a locator-free subject. A locator-keyed
    rule then observes indeterminate evidence and the enforcement boundary
    fails closed, so an advance to deny can never pass an early recheck
    through the stand-in locator as if it were real evidence (parked
    finding D1).
    """

    def __init__(self, *, enforcement: PolicyPreflightEnforcement) -> None:
        self._enforcement = enforcement
        self._stand_in = multipart_recheck_locator_stand_in()

    async def authorize_small_file(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> AllowedPolicyRevisionBinding:
        locator = preflight.normalized_locator
        carries_real_locator = locator is not None and not _is_locator_free_recheck(
            preflight, self._stand_in
        )
        subject = PolicySubject(
            workspace_id=device_context.workspace_id,
            source_id=preflight.source_id,
            normalized_locator=locator.value if carries_real_locator else None,
            media_type=preflight.media_type,
            size_bytes=preflight.size_bytes,
        )
        return await self._enforcement.authorize_preflight(
            subject=subject,
            boundary=PolicyBoundary.MULTIPART_UPLOAD,
            context=diagnostic_context,
        )


# --- the validated staging-key str seam ------------------------------------------


def validated_staging_key(staging_key: str) -> MultipartStagingKey:
    """Validate one persisted staging key against the closed grammar.

    The store only persists keys the creation boundary derived, so a key
    that fails the grammar here is corrupted state surfaced as the closed
    provider-state rejection — never a value that could address a canonical
    object (``MultipartStagingKey.parse`` fails closed on every canonical
    ``objects/sha256/...`` shape).
    """

    try:
        return MultipartStagingKey.parse(staging_key)
    except ValueError as cause:
        raise MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID) from cause


class ValidatedStagingKeyProvider(Protocol):
    """The R2 multipart adapter's six-method shape over validated keys."""

    async def create_upload(
        self, staging_key: MultipartStagingKey
    ) -> MultipartProviderUploadId: ...

    async def presign_part(
        self,
        staging_key: MultipartStagingKey,
        upload_id: MultipartProviderUploadId,
        part_range: MultipartPartRange,
    ) -> MultipartPartUrl: ...

    async def list_parts(
        self, staging_key: MultipartStagingKey, upload_id: MultipartProviderUploadId
    ) -> tuple[MultipartProviderPart, ...]: ...

    async def complete_upload(
        self,
        staging_key: MultipartStagingKey,
        upload_id: MultipartProviderUploadId,
        parts: Sequence[MultipartProviderPart],
    ) -> None: ...

    async def abort_upload(
        self, staging_key: MultipartStagingKey, upload_id: MultipartProviderUploadId
    ) -> None: ...

    async def delete_staging_object(self, staging_key: MultipartStagingKey) -> None: ...


class ValidatedStagingKeyMultipartProvider:
    """Bind a validated-key staging provider onto the service's str seam.

    Every method re-validates the persisted str staging key against the
    closed grammar before it crosses to the adapter and translates the
    provider part facts between the adapter's and the domain's value
    objects. Only exact identities cross — never a listing, prefix or
    wildcard authority.
    """

    def __init__(self, provider: ValidatedStagingKeyProvider) -> None:
        self._provider = provider

    async def create_upload(self, staging_key: str) -> MultipartProviderUploadId:
        return await self._provider.create_upload(validated_staging_key(staging_key))

    async def presign_part(
        self,
        staging_key: str,
        upload_id: MultipartProviderUploadId,
        part_range: MultipartPartRange,
    ) -> MultipartPartUrl:
        return await self._provider.presign_part(
            validated_staging_key(staging_key), upload_id, part_range
        )

    async def list_parts(
        self, staging_key: str, upload_id: MultipartProviderUploadId
    ) -> tuple[MultipartObservedPart, ...]:
        parts = await self._provider.list_parts(validated_staging_key(staging_key), upload_id)
        return tuple(
            MultipartObservedPart(
                part_number=part.part_number,
                etag=part.etag,
                size_bytes=part.size_bytes,
            )
            for part in parts
        )

    async def complete_upload(
        self,
        staging_key: str,
        upload_id: MultipartProviderUploadId,
        parts: Sequence[MultipartObservedPart],
    ) -> None:
        await self._provider.complete_upload(
            validated_staging_key(staging_key),
            upload_id,
            tuple(
                MultipartProviderPart(
                    part_number=part.part_number,
                    etag=part.etag,
                    size_bytes=part.size_bytes,
                )
                for part in parts
            ),
        )

    async def abort_upload(self, staging_key: str, upload_id: MultipartProviderUploadId) -> None:
        await self._provider.abort_upload(validated_staging_key(staging_key), upload_id)

    async def delete_staging_object(self, staging_key: str) -> None:
        await self._provider.delete_staging_object(validated_staging_key(staging_key))


# --- the offline graph -------------------------------------------------------------


def _offline_clock(state: OfflineMultipartUploadState) -> AwareUtcClock:
    """The offline aware-UTC clock reading the state's mutable frozen moment."""

    def read_now() -> datetime:
        if state.now is not None:
            return state.now
        return datetime.now(UTC)

    return read_now


@dataclass
class OfflineMultipartUploadState:
    """Public knobs and safety counters of the offline multipart graph.

    Tests seed behavior through ``now`` (the frozen clock moment), the two
    policy knobs (``is_policy_denied`` closes every boundary;
    ``locator_keyed_rule_present`` makes the update recheck's locator-free
    subject evaluate indeterminate, proving the fail-closed recheck),
    ``current_reference`` (the update-base resolver), ``staging_preimage``
    (the bytes the staging byte source serves the verification spool) and
    ``delete_staging_error_reason`` (the committed session's inline staging
    delete fails with that closed token). ``publication_commits`` proves
    exactly-once publication; ``provider`` is the composed staging provider
    seam for seeding provider-observed parts; ``rejection_diagnostics_snapshot``
    reads the composed rejection ring.
    """

    now: datetime | None = None
    is_policy_denied: bool = False
    locator_keyed_rule_present: bool = False
    active_policy_revision_number: int = 7
    current_reference: CanonicalSourceReference | None = None
    staging_preimage: bytes | None = None
    delete_staging_error_reason: MultipartRejectionReason | None = None
    publication_commits: int = 0
    published_event_ids: set[UUID] = field(default_factory=set)
    provider: OfflineMultipartStagingProvider | None = field(default=None, repr=False)
    metrics: MultipartRejectionDiagnosticsSource | None = field(default=None, repr=False)

    def require_provider(self) -> OfflineMultipartStagingProvider:
        """Return the composed staging provider (the test seeding seam)."""

        if self.provider is None:
            raise RuntimeError("the offline multipart composition is not bound yet")
        return self.provider

    def rejection_diagnostics_snapshot(self) -> MultipartRejectionDiagnostics:
        """Return the rejection ring snapshot of the composed metrics sink."""

        if self.metrics is None:
            raise RuntimeError("the offline multipart composition is not bound yet")
        return self.metrics.rejection_diagnostics()


@dataclass
class _OfflineMultipartSessionRow:
    """One durable multipart session row as the offline store keeps it."""

    session_id_value: str
    workspace_id: UUID
    device_id: UUID
    preflight: SmallFilePreflight
    operation_id: UUID
    state: MultipartSessionState
    part_size_bytes: int
    part_count: int
    total_size_bytes: int
    expires_at: datetime
    staging_key: str | None = None
    provider_upload_id_value: str | None = None
    completed_parts: dict[int, tuple[str, int]] = field(default_factory=dict)
    terminal_result: SmallFileTerminalResult | None = None
    claim_token: UUID | None = None
    claim_expires_at: datetime | None = None
    cleanup_state: str = "none"
    cleanup_next_retry_at: datetime | None = None

    def is_forward_expired(self, now: datetime) -> bool:
        return self.state in _FORWARD_SESSION_STATES and self.expires_at <= now

    def has_provider_identity(self) -> bool:
        return self.staging_key is not None and self.provider_upload_id_value is not None

    def record(self) -> MultipartSessionRecord:
        return MultipartSessionRecord(
            session_id=MultipartUploadSessionId(self.session_id_value),
            state=self.state,
            part_size_bytes=self.part_size_bytes,
            part_count=self.part_count,
            total_size_bytes=self.total_size_bytes,
            expires_at=self.expires_at,
            staging_key=self.staging_key,
            provider_upload_id=(
                None
                if self.provider_upload_id_value is None
                else MultipartProviderUploadId(self.provider_upload_id_value)
            ),
            completed_part_numbers=frozenset(self.completed_parts),
            terminal_result=self.terminal_result,
        )


def _session_not_found() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_SESSION_NOT_FOUND)


def _session_expired() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_SESSION_EXPIRED)


def _session_state_invalid() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_SESSION_STATE_INVALID)


def _provider_state_invalid() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID)


def _part_invalid() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_PART_INVALID)


def _completion_in_progress() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_COMPLETION_IN_PROGRESS)


class OfflineMultipartEvidenceStore:
    """Evidence store binding each session to its frozen bound operation.

    The offline session store registers the bound operation at reservation
    time — mirroring the durable row's frozen evidence columns — and this
    read side resolves it with the same owner-checked closure the durable
    boundary applies.
    """

    def __init__(self) -> None:
        self.bindings: dict[str, BoundSmallFileOperation] = {}

    def register(
        self, session_id: MultipartUploadSessionId, bound: BoundSmallFileOperation
    ) -> None:
        self.bindings.setdefault(session_id.value, bound)

    async def load_bound_operation(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> BoundSmallFileOperation:
        del diagnostic_context
        bound = self.bindings.get(session_id.value)
        if bound is None:
            raise _session_not_found()
        if (
            bound.workspace_id != device_context.workspace_id
            or bound.device_id != device_context.device_id
        ):
            raise _session_not_found()
        return bound


@dataclass
class _OfflineOperationRow:
    """One durable small-file upload-operation row as the offline store keeps it."""

    operation: SmallFileUploadOperation
    operation_id: UUID


class OfflineMultipartOperationStore:
    """Operation store with identity-keyed exact reservation replay."""

    def __init__(self, clock: AwareUtcClock) -> None:
        self._clock = clock
        self.rows: list[_OfflineOperationRow] = []

    async def resolve_terminal_result(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileTerminalResult | None:
        del preflight, device_context, diagnostic_context
        return None

    async def reserve_operation(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        policy_binding: AllowedPolicyRevisionBinding,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileUploadOperation:
        del diagnostic_context
        if policy_binding.workspace_id != device_context.workspace_id:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        for row in self.rows:
            same_identity = (
                row.operation.device_context.device_id == device_context.device_id
                and row.operation.preflight.event_id == preflight.event_id
                and row.operation.preflight.idempotency_key == preflight.idempotency_key
            )
            if same_identity:
                stored = row.operation.preflight
                if (
                    stored.operation is not preflight.operation
                    or stored.sha256 != preflight.sha256
                    or stored.size_bytes != preflight.size_bytes
                    or stored.media_type != preflight.media_type
                    or stored.source_id != preflight.source_id
                    or stored.base_version_id != preflight.base_version_id
                ):
                    raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH)
                return row.operation
        operation = SmallFileUploadOperation(
            operation_token=UploadOperationToken(secrets.token_urlsafe(32)),
            preflight=preflight,
            device_context=device_context,
            reserved_source_id=(
                uuid4() if preflight.operation is SmallFileOperation.CREATE else None
            ),
            expires_at=self._clock() + timedelta(hours=24),
        )
        self.rows.append(_OfflineOperationRow(operation=operation, operation_id=uuid4()))
        return operation

    async def record_terminal_result(
        self,
        operation: SmallFileUploadOperation,
        result: SmallFileTerminalResult,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del operation, result, diagnostic_context
        return None

    async def resolve_bound_operation(
        self,
        operation_token: UploadOperationToken,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> BoundSmallFileOperation:
        del operation_token, device_context, diagnostic_context
        raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND)

    async def record_bound_terminal_result(
        self,
        bound: BoundSmallFileOperation,
        result: SmallFileTerminalResult,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del bound, result, diagnostic_context
        return None

    async def record_bound_terminal_failure(
        self,
        bound: BoundSmallFileOperation,
        error_code: ErrorCode,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del bound, error_code, diagnostic_context
        return None


class OfflineMultipartSessionStore:
    """In-memory session store mirroring the durable adapter semantics."""

    def __init__(
        self,
        state: OfflineMultipartUploadState,
        evidence: OfflineMultipartEvidenceStore,
        clock: AwareUtcClock,
    ) -> None:
        self._state = state
        self._evidence = evidence
        self._clock = clock
        self.rows: list[_OfflineMultipartSessionRow] = []

    def _row(self, session_id: MultipartUploadSessionId) -> _OfflineMultipartSessionRow:
        for row in self.rows:
            if row.session_id_value == session_id.value:
                return row
        raise _session_not_found()

    def _require_owner(
        self, row: _OfflineMultipartSessionRow, device_context: SmallFileDeviceContext
    ) -> None:
        if (
            row.workspace_id != device_context.workspace_id
            or row.device_id != device_context.device_id
        ):
            raise _session_not_found()

    def _register_evidence(
        self, row: _OfflineMultipartSessionRow, operation: SmallFileUploadOperation
    ) -> None:
        """Bind the session row to its frozen bound-operation evidence."""

        is_create = row.preflight.operation is SmallFileOperation.CREATE
        locator = row.preflight.normalized_locator if is_create else None
        bound = BoundSmallFileOperation(
            operation_id=operation.reserved_source_id
            if operation.reserved_source_id is not None
            else uuid4(),
            operation_token=operation.operation_token,
            workspace_id=row.workspace_id,
            device_id=row.device_id,
            event_id=row.preflight.event_id,
            idempotency_key=row.preflight.idempotency_key,
            operation=row.preflight.operation,
            declared_sha256=row.preflight.sha256,
            declared_size_bytes=row.preflight.size_bytes,
            declared_media_type=row.preflight.media_type,
            policy_revision_number=row.preflight.policy_revision_number,
            reserved_source_id=operation.reserved_source_id,
            update_source_id=row.preflight.source_id,
            update_base_version_id=row.preflight.base_version_id,
            normalized_locator=locator,
            locator_fingerprint=(
                compute_locator_fingerprint(locator) if locator is not None else None
            ),
            expires_at=row.expires_at,
            terminal_result=None,
        )
        self._evidence.register(MultipartUploadSessionId(row.session_id_value), bound)

    async def reserve_session(
        self,
        *,
        operation: SmallFileUploadOperation,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionRecord:
        del diagnostic_context
        for row in self.rows:
            same_identity = (
                row.workspace_id == device_context.workspace_id
                and row.device_id == device_context.device_id
                and row.preflight.event_id == operation.preflight.event_id
                and row.preflight.idempotency_key == operation.preflight.idempotency_key
            )
            if same_identity:
                return row.record()
        size_bytes = operation.preflight.size_bytes
        part_count = -(-size_bytes // _OFFLINE_PART_SIZE_BYTES)
        now = self._clock()
        row = _OfflineMultipartSessionRow(
            session_id_value=secrets.token_urlsafe(32),
            workspace_id=device_context.workspace_id,
            device_id=device_context.device_id,
            preflight=operation.preflight,
            operation_id=uuid4(),
            state=MultipartSessionState.CREATED,
            part_size_bytes=_OFFLINE_PART_SIZE_BYTES,
            part_count=part_count,
            total_size_bytes=size_bytes,
            expires_at=compute_multipart_session_expiry(now),
        )
        self.rows.append(row)
        self._register_evidence(row, operation)
        return row.record()

    async def record_provider_identity(
        self,
        *,
        session_id: MultipartUploadSessionId,
        staging_key: str,
        provider_upload_id: MultipartProviderUploadId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionRecord:
        del diagnostic_context
        row = self._row(session_id)
        self._require_owner(row, device_context)
        if (
            row.staging_key == staging_key
            and row.provider_upload_id_value == provider_upload_id.value
        ):
            return row.record()
        if row.state not in _PART_RECORDING_STATES or row.is_forward_expired(self._clock()):
            raise _session_state_invalid()
        if row.has_provider_identity():
            raise _provider_state_invalid()
        row.staging_key = staging_key
        row.provider_upload_id_value = provider_upload_id.value
        return row.record()

    async def load_owned_session(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionRecord:
        del diagnostic_context
        row = self._row(session_id)
        self._require_owner(row, device_context)
        if row.is_forward_expired(self._clock()):
            raise _session_expired()
        return row.record()

    async def record_provider_part(
        self,
        *,
        session_id: MultipartUploadSessionId,
        part_number: int,
        etag: MultipartProviderPartETag,
        verified_size_bytes: int,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del diagnostic_context
        row = self._row(session_id)
        self._require_owner(row, device_context)
        if not row.has_provider_identity() or row.state not in _PART_RECORDING_STATES:
            raise _session_state_invalid()
        if row.is_forward_expired(self._clock()):
            raise _session_expired()
        if not 1 <= part_number <= row.part_count:
            raise _part_invalid()
        window_size = (
            _OFFLINE_PART_SIZE_BYTES
            if part_number < row.part_count
            else row.total_size_bytes - (row.part_count - 1) * _OFFLINE_PART_SIZE_BYTES
        )
        if verified_size_bytes != window_size:
            raise _provider_state_invalid()
        observed = row.completed_parts.get(part_number)
        if observed is not None:
            if observed == (etag.value, verified_size_bytes):
                return
            raise _provider_state_invalid()
        row.completed_parts[part_number] = (etag.value, verified_size_bytes)
        if row.state is MultipartSessionState.CREATED:
            row.state = MultipartSessionState.UPLOADING

    async def claim_completion(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionClaim:
        del diagnostic_context
        row = self._row(session_id)
        self._require_owner(row, device_context)
        if row.state is MultipartSessionState.COMMITTED:
            return MultipartSessionClaim(
                session=row.record(), claim_token=None, claim_expires_at=None
            )
        if row.state in _TERMINAL_OBLIGATION_STATES:
            raise _session_state_invalid()
        if not row.has_provider_identity():
            raise _session_state_invalid()
        if row.is_forward_expired(self._clock()):
            raise _session_expired()
        now = self._clock()
        if (
            row.state in _COMPLETION_FAMILY_STATES
            and row.claim_token is not None
            and row.claim_expires_at is not None
            and row.claim_expires_at > now
        ):
            raise _completion_in_progress()
        row.state = MultipartSessionState.COMPLETING
        row.claim_token = uuid4()
        row.claim_expires_at = now + timedelta(seconds=_OFFLINE_COMPLETION_LEASE_SECONDS)
        return MultipartSessionClaim(
            session=row.record(),
            claim_token=row.claim_token,
            claim_expires_at=row.claim_expires_at,
        )

    async def record_terminal_result(
        self,
        *,
        claim: MultipartSessionClaim,
        result: SmallFileTerminalResult | None = None,
        failure_state: MultipartSessionState | None = None,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del diagnostic_context
        if (result is None) == (failure_state is None):
            raise ValueError("exactly one of result or failure_state is required")
        row = self._row(claim.session.session_id)
        if row.state is MultipartSessionState.COMMITTED:
            if result is not None and row.terminal_result == result:
                return
            raise _session_state_invalid()
        if row.state not in _COMPLETION_FAMILY_STATES:
            raise _session_state_invalid()
        if claim.claim_token is None or row.claim_token != claim.claim_token:
            raise _completion_in_progress()
        if row.claim_expires_at is None or row.claim_expires_at <= self._clock():
            raise _completion_in_progress()
        if result is not None:
            row.state = MultipartSessionState.COMMITTED
            row.terminal_result = result
        else:
            assert failure_state is not None
            row.state = failure_state
            row.cleanup_state = "pending"
            row.cleanup_next_retry_at = self._clock()
        row.claim_token = None
        row.claim_expires_at = None

    async def claim_cleanup_batch(
        self,
        *,
        batch_limit: int,
        diagnostic_context: DiagnosticContext,
    ) -> list[MultipartCleanupClaim]:
        del diagnostic_context
        if batch_limit < 1:
            raise ValueError("batch_limit must be positive")
        now = self._clock()
        claims: list[MultipartCleanupClaim] = []
        for row in self.rows:
            if len(claims) >= batch_limit:
                break
            if row.cleanup_state != "pending" or row.cleanup_next_retry_at is None:
                continue
            if row.cleanup_next_retry_at > now:
                continue
            row.cleanup_state = "running"
            row.claim_token = uuid4()
            row.claim_expires_at = now + timedelta(seconds=_OFFLINE_COMPLETION_LEASE_SECONDS)
            claims.append(
                MultipartCleanupClaim(
                    session=row.record(),
                    claim_token=row.claim_token,
                    claim_expires_at=row.claim_expires_at,
                )
            )
        return claims

    async def record_cleanup_result(
        self,
        *,
        claim: MultipartCleanupClaim,
        is_succeeded: bool,
        failure_reason: ErrorCode | None = None,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del diagnostic_context, failure_reason
        row = self._row(claim.session.session_id)
        if row.claim_token != claim.claim_token:
            raise _completion_in_progress()
        if is_succeeded:
            row.state = MultipartSessionState.CLEANED
            row.cleanup_state = "succeeded"
            row.cleanup_next_retry_at = None
        else:
            row.cleanup_state = "failed"
            row.cleanup_next_retry_at = self._clock() + timedelta(minutes=1)
        row.claim_token = None
        row.claim_expires_at = None


class OfflineMultipartPolicyGuard:
    """Offline policy guard honoring the state's two denial knobs.

    ``is_policy_denied`` denies every boundary. ``locator_keyed_rule_present``
    denies exactly the frozen UPDATE recheck — whose subject the guard
    evaluates locator-free — modeling one locator-keyed rule that observes
    indeterminate evidence and fails closed (parked finding D1).
    """

    def __init__(self, state: OfflineMultipartUploadState) -> None:
        self._state = state
        self._stand_in = multipart_recheck_locator_stand_in()

    async def authorize_small_file(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> AllowedPolicyRevisionBinding:
        del diagnostic_context
        if self._state.is_policy_denied:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)
        if self._state.locator_keyed_rule_present and _is_locator_free_recheck(
            preflight, self._stand_in
        ):
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_INDETERMINATE)
        return AllowedPolicyRevisionBinding(
            workspace_id=device_context.workspace_id,
            policy_revision_number=self._state.active_policy_revision_number,
        )


class OfflineMultipartCurrentSourceStore:
    """Current-source resolver over the state's seeded reference."""

    def __init__(self, state: OfflineMultipartUploadState) -> None:
        self._state = state

    async def resolve_current(
        self, command: ReadCurrentSourceCommand, diagnostic_context: DiagnosticContext
    ) -> CanonicalSourceReference:
        del diagnostic_context
        reference = self._state.current_reference
        if reference is None:
            raise CanonicalReadStateError(source_id=command.source_id)
        return reference


class OfflineMultipartStagingProvider:
    """Staging provider over the validated str-key seam, fully in memory.

    The same ``MultipartStagingKey`` validation the R2 adapter applies gates
    every method, so an offline test cannot drive a canonical-shaped key
    through the seam either. ``upload_part`` is the seeding seam for
    provider-observed completed parts.
    """

    def __init__(self, state: OfflineMultipartUploadState, clock: AwareUtcClock) -> None:
        self._state = state
        self._clock = clock
        self.uploads: dict[str, str] = {}
        self.objects: set[str] = set()
        self.parts: dict[str, dict[int, tuple[str, int]]] = {}

    def upload_part(self, staging_key: str, part_number: int, size_bytes: int) -> None:
        """Seed one provider-observed completed part of a live upload."""

        upload_id = self.uploads[staging_key]
        self.parts.setdefault(upload_id, {})[part_number] = (f"etag-{part_number}", size_bytes)

    async def create_upload(self, staging_key: str) -> MultipartProviderUploadId:
        key = validated_staging_key(staging_key)
        upload_id = f"upload-{secrets.token_urlsafe(16)}"
        self.uploads[key.value] = upload_id
        self.parts[upload_id] = {}
        return MultipartProviderUploadId(upload_id)

    async def presign_part(
        self,
        staging_key: str,
        upload_id: MultipartProviderUploadId,
        part_range: MultipartPartRange,
    ) -> MultipartPartUrl:
        key = validated_staging_key(staging_key)
        if self.uploads.get(key.value) != upload_id.value:
            raise _provider_state_invalid()
        return MultipartPartUrl(
            part_number=part_range.part_number,
            byte_range=part_range,
            url=f"{_OFFLINE_STAGING_URL_PREFIX}{secrets.token_urlsafe(24)}",
            expires_at=self._clock() + timedelta(minutes=10),
        )

    async def list_parts(
        self, staging_key: str, upload_id: MultipartProviderUploadId
    ) -> tuple[MultipartObservedPart, ...]:
        key = validated_staging_key(staging_key)
        if self.uploads.get(key.value) != upload_id.value:
            raise _provider_state_invalid()
        return tuple(
            MultipartObservedPart(
                part_number=part_number,
                etag=MultipartProviderPartETag(etag),
                size_bytes=size_bytes,
            )
            for part_number, (etag, size_bytes) in sorted(self.parts[upload_id.value].items())
        )

    async def complete_upload(
        self,
        staging_key: str,
        upload_id: MultipartProviderUploadId,
        parts: Sequence[MultipartObservedPart],
    ) -> None:
        key = validated_staging_key(staging_key)
        if not parts:
            raise _provider_state_invalid()
        if self.uploads.get(key.value) != upload_id.value:
            raise _provider_state_invalid()
        self.objects.add(key.value)
        del self.uploads[key.value]
        self.parts.pop(upload_id.value, None)

    async def abort_upload(self, staging_key: str, upload_id: MultipartProviderUploadId) -> None:
        key = validated_staging_key(staging_key)
        if self.uploads.get(key.value) == upload_id.value:
            del self.uploads[key.value]
            self.parts.pop(upload_id.value, None)

    async def delete_staging_object(self, staging_key: str) -> None:
        key = validated_staging_key(staging_key)
        reason = self._state.delete_staging_error_reason
        if reason is not None:
            raise MultipartUploadError(ErrorCode(reason.value))
        self.objects.discard(key.value)


class OfflineMultipartStagingByteSource:
    """Staging read seam serving the state's seeded preimage bytes."""

    def __init__(self, state: OfflineMultipartUploadState) -> None:
        self._state = state

    def open_staging_stream(
        self, staging_key: str
    ) -> AbstractAsyncContextManager[AsyncIterable[bytes]]:
        @asynccontextmanager
        async def _stream() -> AsyncIterator[AsyncIterable[bytes]]:
            validated_staging_key(staging_key)
            yield _chunked(self._state.staging_preimage or b"")

        return _stream()


def _chunked(content: bytes) -> AsyncIterator[bytes]:
    async def _iterate() -> AsyncIterator[bytes]:
        for start in range(0, len(content), _OFFLINE_STAGING_CHUNK_BYTES):
            yield content[start : start + _OFFLINE_STAGING_CHUNK_BYTES]

    return _iterate()


class _OfflineFailingVerifiedReader:
    """Fail-closed canonical reader: the offline graph serves no canonical bytes."""

    async def read(self, size_bytes: int = 1_048_576) -> bytes:
        del size_bytes
        raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_OBJECT_MISSING)

    def __aiter__(self) -> _OfflineFailingVerifiedReader:
        return self

    async def __anext__(self) -> bytes:
        raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_OBJECT_MISSING)


class OfflineMultipartObjectStore:
    """Object-store double performing the real size/digest spool verification."""

    def __init__(self, clock: AwareUtcClock) -> None:
        self._clock = clock

    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None:
        del expected
        return None

    async def store_stream(
        self,
        stream: AsyncIterable[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt:
        chunks: list[bytes] = []
        async for chunk in stream:
            chunks.append(chunk)
        content = b"".join(chunks)
        computed = ContentDigest.parse(hashlib.sha256(content).hexdigest())
        if claimed_sha256 is not None and computed.hexadecimal != claimed_sha256:
            raise ObjectStorageError(
                ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
                safe_details={"reason": DIGEST_MISMATCH},
            )
        if len(content) != expected_size_bytes:
            raise ObjectStorageError(
                ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
                safe_details={"reason": SIZE_MISMATCH},
            )
        return VerifiedObjectReceipt(
            content_digest=computed,
            object_key=derive_canonical_object_key(computed),
            size_bytes=len(content),
            media_type=CanonicalMediaType.parse(media_type),
            verified_at=self._clock(),
            verification_method=VerificationMethod.UPLOADED_FULL_READ,
        )

    async def verify_existing_object(self, expected: ExpectedObject) -> VerifiedObjectReceipt:
        del expected
        raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_OBJECT_MISSING)

    def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AbstractAsyncContextManager[VerifiedObjectReader]:
        """The offline graph never serves a canonical verified reader.

        The multipart verification path spools the staging stream through
        ``store_stream`` only; a canonical reader request fails closed on
        its first read.
        """

        del expected

        @asynccontextmanager
        async def _reader() -> AsyncIterator[VerifiedObjectReader]:
            yield _OfflineFailingVerifiedReader()

        return _reader()


class OfflineMultipartPublicationGateway:
    """Publication gateway freezing exactly one commit per journal event."""

    def __init__(self, state: OfflineMultipartUploadState, clock: AwareUtcClock) -> None:
        self._state = state
        self._clock = clock
        self._results: dict[UUID, SourceVersionPublicationResult] = {}

    async def publish_create(
        self,
        *,
        command: CreateSourceVersion,
        stream: AsyncIterable[bytes],
        policy_binding: AllowedPolicyRevisionBinding,
        bound_operation: BoundSmallFileOperation,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        del stream, policy_binding, bound_operation, diagnostic_context
        return self._commit(command)

    async def publish_update(
        self,
        *,
        command: UpdateSourceVersion,
        stream: AsyncIterable[bytes],
        policy_binding: AllowedPolicyRevisionBinding,
        bound_operation: BoundSmallFileOperation,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        del stream, policy_binding, bound_operation, diagnostic_context
        return self._commit(command)

    def _commit(
        self, command: CreateSourceVersion | UpdateSourceVersion
    ) -> SourceVersionPublicationResult:
        frozen = self._results.get(command.event_id)
        if frozen is not None:
            return frozen
        if command.event_id not in self._state.published_event_ids:
            self._state.published_event_ids.add(command.event_id)
            self._state.publication_commits += 1
        result = SourceVersionPublicationResult(
            source_id=command.source_id,
            source_version_id=uuid4(),
            content_version=1,
            event_id=command.event_id,
            event_sequence=1,
            content_digest=command.expected_object.content_digest,
            outcome=PublicationOutcome.PUBLISHED,
            committed_at=self._clock(),
        )
        self._results[command.event_id] = result
        return result


def compose_offline_multipart_upload(
    *,
    state: OfflineMultipartUploadState | None = None,
    metrics: MultipartUploadMetricsWithRejectionDiagnostics | None = None,
) -> MultipartUploadRuntime:
    """Build the deterministic offline multipart upload runtime."""

    offline_state = state if state is not None else OfflineMultipartUploadState()
    clock = _offline_clock(offline_state)
    recorder = metrics if metrics is not None else InMemoryMultipartUploadMetrics()
    offline_state.metrics = recorder
    evidence_store = OfflineMultipartEvidenceStore()
    staging_provider = OfflineMultipartStagingProvider(offline_state, clock)
    offline_state.provider = staging_provider
    service = MultipartUploadService(
        session_store=OfflineMultipartSessionStore(offline_state, evidence_store, clock),
        evidence_store=evidence_store,
        operation_store=OfflineMultipartOperationStore(clock),
        policy_guard=OfflineMultipartPolicyGuard(offline_state),
        current_sources=OfflineMultipartCurrentSourceStore(offline_state),
        publication_gateway=OfflineMultipartPublicationGateway(offline_state, clock),
        object_store=OfflineMultipartObjectStore(clock),
        staging_provider=staging_provider,
        staging_byte_source=OfflineMultipartStagingByteSource(offline_state),
        metrics=recorder,
        clock=clock,
    )
    return MultipartUploadRuntime(service=service, rejection_diagnostics=recorder)


__all__ = [
    "MultipartUploadRuntime",
    "OfflineMultipartCurrentSourceStore",
    "OfflineMultipartEvidenceStore",
    "OfflineMultipartObjectStore",
    "OfflineMultipartOperationStore",
    "OfflineMultipartPolicyGuard",
    "OfflineMultipartPublicationGateway",
    "OfflineMultipartSessionStore",
    "OfflineMultipartStagingByteSource",
    "OfflineMultipartStagingProvider",
    "OfflineMultipartUploadState",
    "RecheckLocatorAwarePolicyEnforcementGuard",
    "ValidatedStagingKeyMultipartProvider",
    "compose_offline_multipart_upload",
    "multipart_recheck_locator_stand_in",
    "validated_staging_key",
]
