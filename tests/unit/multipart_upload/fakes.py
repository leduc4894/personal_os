"""Narrow in-memory fakes proving the multipart upload service orchestration.

Every fake records the exact port call sequence into one shared ledger so a
test can assert the full cross-port order of the spec 6.1/6.3 flows — the
persist-before-create reservation, the fenced provider-identity write, part
reconciliation, the claimed completion chain (recheck, ListParts, complete,
verification spool, publication, frozen terminal write, exact staging delete)
and the exact cleanup execution — with closed string entries only.

The session-store fake mirrors the durable PostgreSQL semantics of the Task 3
adapter: identity-keyed lifetime-unique reservations, an idempotent fenced
provider-identity write that rejects divergence, owner/expiry closure, part
facts admitted only inside their state family and geometry window, a finite
completion lease with token rotation on expiry, compare-and-set terminal
writes that converge an identical committed replay, and the expiry-strike plus
lease-fenced cleanup obligation lifecycle. The staging provider fake mirrors
the Task 4 capability boundary: six str-keyed methods, NoSuchUpload-shaped
absence on ``list_parts`` once an upload is completed or aborted, and
idempotent-absence abort/delete. The object-store fake performs the real
size/digest verification over the streamed staging bytes, so the verification
spool path is genuinely exercised. No fake retains or echoes staging keys,
presigned URLs, digests or provider identities in ledgers or assertions.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterable, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.enforcement import AllowedPolicyRevisionBinding
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.multipart_upload.contracts import (
    MultipartPartGeometry,
    MultipartPartRange,
    MultipartPartUrl,
    MultipartSessionState,
    MultipartUploadSessionId,
    compute_multipart_session_expiry,
)
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.metrics import InMemoryMultipartUploadMetrics
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
    derive_staging_key,
)
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from personal_os.object_storage.errors import DIGEST_MISMATCH, SIZE_MISMATCH, ObjectStorageError
from personal_os.small_file_sync.contracts import (
    BoundSmallFileOperation,
    NormalizedLocator,
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
    SmallFileUploadOperation,
    UploadOperationToken,
    compute_locator_fingerprint,
)
from personal_os.small_file_sync.errors import SmallFileSyncError
from personal_os.sources.commands import SourceType
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.reading import (
    CanonicalReadStateError,
    CanonicalSourceReference,
    ReadCurrentSourceCommand,
)
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult

#: Shared ledger entry constants: one closed string per observed port call.
SESSION_RESERVE: Final[str] = "session_store.reserve_session"
SESSION_RECORD_IDENTITY: Final[str] = "session_store.record_provider_identity"
SESSION_LOAD: Final[str] = "session_store.load_owned_session"
SESSION_RECORD_PART: Final[str] = "session_store.record_provider_part"
SESSION_CLAIM_COMPLETION: Final[str] = "session_store.claim_completion"
SESSION_RECORD_TERMINAL: Final[str] = "session_store.record_terminal_result"
SESSION_CLAIM_CLEANUP: Final[str] = "session_store.claim_cleanup_batch"
SESSION_RECORD_CLEANUP: Final[str] = "session_store.record_cleanup_result"
EVIDENCE_LOAD: Final[str] = "evidence_store.load_bound_operation"
OPERATION_RESERVE: Final[str] = "operation_store.reserve_operation"
POLICY_GUARD: Final[str] = "policy_guard.authorize_small_file"
CURRENT_SOURCES_RESOLVE: Final[str] = "current_sources.resolve_current"
PUBLISH_CREATE: Final[str] = "publish_create"
PUBLISH_UPDATE: Final[str] = "publish_update"
OBJECT_STORE_RESOLVE: Final[str] = "object_store.resolve_verified_object"
OBJECT_STORE_STORE_STREAM: Final[str] = "object_store.store_stream"
STAGING_OPEN_STREAM: Final[str] = "staging_source.open_staging_stream"
PROVIDER_CREATE_UPLOAD: Final[str] = "provider.create_upload"
PROVIDER_PRESIGN_PART: Final[str] = "provider.presign_part"
PROVIDER_LIST_PARTS: Final[str] = "provider.list_parts"
PROVIDER_COMPLETE_UPLOAD: Final[str] = "provider.complete_upload"
PROVIDER_ABORT_UPLOAD: Final[str] = "provider.abort_upload"
PROVIDER_DELETE_STAGING: Final[str] = "provider.delete_staging_object"

#: The default harness transfer: 20 MiB over three parts (8 MiB, 8 MiB, 4 MiB).
DEFAULT_MULTIPART_SIZE_BYTES: Final[int] = 20 * 1024 * 1024
DEFAULT_PART_COUNT: Final[int] = 3

#: The finite completion lease of the durable store, mirrored for expiry tests.
COMPLETION_LEASE_SECONDS: Final[int] = 600

_DEFAULT_MEDIA_TYPE: Final[CanonicalMediaType] = CanonicalMediaType.parse("text/markdown")
_CHUNK_BYTES: Final[int] = 1024 * 1024


@dataclass
class CallLedger:
    """Append-only shared record of observed port calls across all fakes."""

    entries: list[str] = field(default_factory=list)

    def record(self, entry: str) -> None:
        self.entries.append(entry)

    def count(self, entry: str) -> int:
        return self.entries.count(entry)

    def first_index(self, entry: str) -> int:
        return self.entries.index(entry)


@dataclass
class MutableUtcClock:
    """Injectable aware-UTC clock whose moment tests advance deterministically."""

    now: datetime = field(default_factory=lambda: datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


def build_diagnostic_context() -> DiagnosticContext:
    """A fresh server-owned diagnostic context for one request-bound unit of work."""

    return create_diagnostic_context().context


def build_device_context() -> SmallFileDeviceContext:
    """A fresh credential-derived device context."""

    return SmallFileDeviceContext(device_id=uuid4(), workspace_id=uuid4())


def build_update_preflight(
    *,
    content_digest: ContentDigest,
    size_bytes: int = DEFAULT_MULTIPART_SIZE_BYTES,
    policy_revision_number: int = 7,
) -> SmallFilePreflight:
    """A valid update preflight inside the multipart routing range."""

    return SmallFilePreflight(
        event_id=uuid4(),
        idempotency_key=SmallFileIdempotencyKey(str(uuid4())),
        operation=SmallFileOperation.UPDATE,
        local_file_id=uuid4(),
        source_id=uuid4(),
        base_version_id=uuid4(),
        normalized_locator=NormalizedLocator("notes/multipart-note.md"),
        sha256=content_digest,
        size_bytes=size_bytes,
        media_type=_DEFAULT_MEDIA_TYPE,
        policy_revision_number=policy_revision_number,
    )


def build_current_reference(
    preflight: SmallFilePreflight,
    *,
    source_version_id: UUID | None = None,
) -> CanonicalSourceReference:
    """The current version of the update target, with an overridable base."""

    return CanonicalSourceReference(
        workspace_id=uuid4(),
        source_id=preflight.source_id if preflight.source_id is not None else uuid4(),
        source_version_id=source_version_id
        if source_version_id is not None
        else (preflight.base_version_id if preflight.base_version_id is not None else uuid4()),
        content_version=3,
        source_type=SourceType.MARKDOWN,
        expected_object=ExpectedObject(
            content_digest=preflight.sha256,
            size_bytes=preflight.size_bytes,
            media_type=preflight.media_type,
        ),
        committed_at=datetime(2026, 8, 28, 9, 30, 0, tzinfo=UTC),
    )


class AllowAllSmallFilePolicyGuard:
    """Policy-guard fake returning the server-owned revision binding."""

    def __init__(self, *, revision_number: int = 7) -> None:
        self._revision_number = revision_number

    async def authorize_small_file(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> AllowedPolicyRevisionBinding:
        del preflight, diagnostic_context
        return AllowedPolicyRevisionBinding(
            workspace_id=device_context.workspace_id,
            policy_revision_number=self._revision_number,
        )


class DenyingSmallFilePolicyGuard:
    """Policy-guard fake raising the typed exclusion-policy denial."""

    def __init__(self, *, system_failure: bool = False) -> None:
        self._system_failure = system_failure

    async def authorize_small_file(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> AllowedPolicyRevisionBinding:
        del preflight, device_context, diagnostic_context
        raise ExclusionPolicyError(
            ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
            if self._system_failure
            else ErrorCode.EXCLUSION_POLICY_DENIED
        )


class FakeCanonicalSourceReadStore:
    """Current-source resolver fake with a scriptable stale or missing base."""

    def __init__(self, reference: CanonicalSourceReference | None) -> None:
        self.reference = reference
        self.raise_state_error = False

    async def resolve_current(
        self, command: ReadCurrentSourceCommand, diagnostic_context: DiagnosticContext
    ) -> CanonicalSourceReference:
        del diagnostic_context
        if self.raise_state_error:
            raise CanonicalReadStateError(source_id=command.source_id)
        if self.reference is None:
            raise CanonicalReadStateError(source_id=command.source_id)
        return self.reference


@dataclass
class _FakeOperationRow:
    """One durable small-file upload-operation row as the fake keeps it."""

    operation: SmallFileUploadOperation
    operation_id: UUID
    fingerprint: tuple[UUID, UUID, UUID, str, str, int, str, int, str]


class FakeSmallFileUploadOperationStore:
    """Operation-store fake with identity-keyed exact reservation replay."""

    def __init__(self, ledger: CallLedger) -> None:
        self._ledger = ledger
        self.rows: list[_FakeOperationRow] = []

    @staticmethod
    def _fingerprint(
        preflight: SmallFilePreflight, device_context: SmallFileDeviceContext
    ) -> tuple[UUID, UUID, UUID, str, str, int, str, int, str]:
        return (
            device_context.workspace_id,
            device_context.device_id,
            preflight.event_id,
            preflight.idempotency_key.value,
            preflight.sha256.hexadecimal,
            preflight.size_bytes,
            preflight.media_type.value,
            preflight.policy_revision_number,
            preflight.base_version_id.hex if preflight.base_version_id else "",
        )

    async def reserve_operation(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        policy_binding: AllowedPolicyRevisionBinding,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileUploadOperation:
        self._ledger.record(OPERATION_RESERVE)
        del policy_binding, diagnostic_context
        fingerprint = self._fingerprint(preflight, device_context)
        for row in self.rows:
            identity_matches = (
                row.operation.preflight.event_id == preflight.event_id
                and row.operation.preflight.idempotency_key == preflight.idempotency_key
                and row.operation.device_context.device_id == device_context.device_id
            )
            if identity_matches:
                if row.fingerprint != fingerprint:
                    raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH)
                return row.operation
        token = UploadOperationToken(secrets.token_urlsafe(32))
        operation = SmallFileUploadOperation(
            operation_token=token,
            preflight=preflight,
            device_context=device_context,
            reserved_source_id=None,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        self.rows.append(
            _FakeOperationRow(
                operation=operation,
                operation_id=uuid4(),
                fingerprint=fingerprint,
            )
        )
        return operation


class FakeMultipartSessionEvidenceStore:
    """Evidence fake binding each session to its frozen bound operation."""

    def __init__(self, ledger: CallLedger) -> None:
        self._ledger = ledger
        self.bindings: dict[str, BoundSmallFileOperation] = {}

    def register(
        self, session_id: MultipartUploadSessionId, bound: BoundSmallFileOperation
    ) -> None:
        self.bindings[session_id.value] = bound

    async def load_bound_operation(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> BoundSmallFileOperation:
        self._ledger.record(EVIDENCE_LOAD)
        del diagnostic_context
        bound = self.bindings.get(session_id.value)
        if bound is None:
            raise MultipartUploadError(ErrorCode.MULTIPART_SESSION_NOT_FOUND)
        if bound.workspace_id != device_context.workspace_id or bound.device_id != (
            device_context.device_id
        ):
            raise MultipartUploadError(ErrorCode.MULTIPART_SESSION_NOT_FOUND)
        return bound


@dataclass
class _FakeSessionRow:
    """One durable multipart session row as the fake store keeps it."""

    session_id_value: str
    workspace_id: UUID
    device_id: UUID
    preflight: SmallFilePreflight
    reserved_source_id: UUID | None
    update_source_id: UUID | None
    update_base_version_id: UUID | None
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
    cleanup_attempt_count: int = 0
    cleanup_next_retry_at: datetime | None = None
    cleanup_reason_code: str | None = None

    def geometry(self) -> MultipartPartGeometry:
        return MultipartPartGeometry(
            total_size_bytes=self.total_size_bytes,
            part_size_bytes=self.part_size_bytes,
            part_count=self.part_count,
        )

    def has_provider_identity(self) -> bool:
        return self.staging_key is not None and self.provider_upload_id_value is not None

    def is_forward_expired(self, now: datetime) -> bool:
        return (
            self.state
            in (
                MultipartSessionState.CREATED,
                MultipartSessionState.UPLOADING,
                MultipartSessionState.COMPLETING,
                MultipartSessionState.VERIFYING,
                MultipartSessionState.PROMOTING,
            )
            and self.expires_at <= now
        )


_FORWARD_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CREATED,
        MultipartSessionState.UPLOADING,
        MultipartSessionState.COMPLETING,
        MultipartSessionState.VERIFYING,
        MultipartSessionState.PROMOTING,
    }
)
_COMPLETION_FAMILY: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.COMPLETING,
        MultipartSessionState.VERIFYING,
        MultipartSessionState.PROMOTING,
    }
)
_PART_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CREATED,
        MultipartSessionState.UPLOADING,
        MultipartSessionState.COMPLETING,
    }
)
_IDENTITY_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {MultipartSessionState.CREATED, MultipartSessionState.UPLOADING}
)
_FAILURE_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CANCELLING,
        MultipartSessionState.EXPIRED,
        MultipartSessionState.INTEGRITY_FAILED,
        MultipartSessionState.POLICY_DENIED,
        MultipartSessionState.CLEANUP_PENDING,
        MultipartSessionState.CLEANED,
    }
)


def _not_found() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_SESSION_NOT_FOUND)


def _expired() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_SESSION_EXPIRED)


def _state_invalid() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_SESSION_STATE_INVALID)


def _provider_state_invalid() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID)


def _in_progress() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_COMPLETION_IN_PROGRESS)


class FakeMultipartSessionStore:
    """Session-store fake mirroring the durable Task 3 adapter semantics."""

    def __init__(self, ledger: CallLedger, clock: Callable[[], datetime]) -> None:
        self._ledger = ledger
        self._clock = clock
        self.rows: list[_FakeSessionRow] = []
        self.cleanup_results: list[tuple[str, bool, str | None]] = []
        #: Test knob: land this competing identity before the next fenced
        #: identity write so the caller's own fresh identity diverges.
        self.divergent_identity_injection: str | None = None

    def _row(self, session_id: MultipartUploadSessionId) -> _FakeSessionRow:
        for row in self.rows:
            if row.session_id_value == session_id.value:
                return row
        raise _not_found()

    def _require_owner(self, row: _FakeSessionRow, device_context: SmallFileDeviceContext) -> None:
        if (
            row.workspace_id != device_context.workspace_id
            or row.device_id != device_context.device_id
        ):
            raise _not_found()

    def _require_forward_alive(self, row: _FakeSessionRow) -> None:
        if row.state not in _FORWARD_STATES:
            raise _state_invalid()
        if row.is_forward_expired(self._clock()):
            raise _expired()

    def record(self, row: _FakeSessionRow) -> MultipartSessionRecord:
        return MultipartSessionRecord(
            session_id=MultipartUploadSessionId(row.session_id_value),
            state=row.state,
            part_size_bytes=row.part_size_bytes,
            part_count=row.part_count,
            total_size_bytes=row.total_size_bytes,
            expires_at=row.expires_at,
            staging_key=row.staging_key,
            provider_upload_id=(
                None
                if row.provider_upload_id_value is None
                else MultipartProviderUploadId(row.provider_upload_id_value)
            ),
            completed_part_numbers=frozenset(row.completed_parts),
            terminal_result=row.terminal_result,
        )

    def cleanup_state_of(self, session_id: MultipartUploadSessionId) -> str:
        return self._row(session_id).cleanup_state

    async def reserve_session(
        self,
        *,
        operation: SmallFileUploadOperation,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionRecord:
        self._ledger.record(SESSION_RESERVE)
        del diagnostic_context
        for row in self.rows:
            same_identity = (
                row.workspace_id == device_context.workspace_id
                and row.device_id == device_context.device_id
                and row.preflight.event_id == operation.preflight.event_id
                and row.preflight.idempotency_key == operation.preflight.idempotency_key
            )
            if same_identity:
                return self.record(row)
        created_at = self._clock()
        row = _FakeSessionRow(
            session_id_value=secrets.token_urlsafe(32),
            workspace_id=device_context.workspace_id,
            device_id=device_context.device_id,
            preflight=operation.preflight,
            reserved_source_id=operation.reserved_source_id,
            update_source_id=operation.preflight.source_id,
            update_base_version_id=operation.preflight.base_version_id,
            state=MultipartSessionState.CREATED,
            part_size_bytes=8 * 1024 * 1024,
            part_count=-(-operation.preflight.size_bytes // (8 * 1024 * 1024)),
            total_size_bytes=operation.preflight.size_bytes,
            expires_at=compute_multipart_session_expiry(created_at),
        )
        self.rows.append(row)
        return self.record(row)

    async def record_provider_identity(
        self,
        *,
        session_id: MultipartUploadSessionId,
        staging_key: str,
        provider_upload_id: MultipartProviderUploadId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionRecord:
        self._ledger.record(SESSION_RECORD_IDENTITY)
        del diagnostic_context
        row = self._row(session_id)
        self._require_owner(row, device_context)
        if self.divergent_identity_injection is not None and not row.has_provider_identity():
            row.staging_key = self.divergent_identity_injection
            row.provider_upload_id_value = "competing-provider-upload-id"
            self.divergent_identity_injection = None
        if (
            row.staging_key == staging_key
            and row.provider_upload_id_value == provider_upload_id.value
        ):
            return self.record(row)
        if row.state not in _IDENTITY_STATES:
            raise _state_invalid()
        if row.is_forward_expired(self._clock()):
            raise _expired()
        if row.has_provider_identity():
            raise _provider_state_invalid()
        row.staging_key = staging_key
        row.provider_upload_id_value = provider_upload_id.value
        return self.record(row)

    async def load_owned_session(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionRecord:
        self._ledger.record(SESSION_LOAD)
        del diagnostic_context
        row = self._row(session_id)
        self._require_owner(row, device_context)
        if row.is_forward_expired(self._clock()):
            raise _expired()
        return self.record(row)

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
        self._ledger.record(SESSION_RECORD_PART)
        del diagnostic_context
        row = self._row(session_id)
        self._require_owner(row, device_context)
        if not row.has_provider_identity():
            raise _state_invalid()
        if row.state not in _PART_STATES:
            raise _state_invalid()
        if row.is_forward_expired(self._clock()):
            raise _expired()
        try:
            window = row.geometry().part_range(part_number)
        except ValueError:
            raise MultipartUploadError(ErrorCode.MULTIPART_PART_INVALID) from None
        if verified_size_bytes != window.size_bytes:
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
        self._ledger.record(SESSION_CLAIM_COMPLETION)
        del diagnostic_context
        row = self._row(session_id)
        self._require_owner(row, device_context)
        if row.state is MultipartSessionState.COMMITTED:
            return MultipartSessionClaim(
                session=self.record(row), claim_token=None, claim_expires_at=None
            )
        if row.state in _FAILURE_STATES:
            raise _state_invalid()
        if not row.has_provider_identity():
            raise _state_invalid()
        if row.is_forward_expired(self._clock()):
            raise _expired()
        now = self._clock()
        if (
            row.state in _COMPLETION_FAMILY
            and row.claim_token is not None
            and row.claim_expires_at is not None
            and row.claim_expires_at > now
        ):
            raise _in_progress()
        row.state = MultipartSessionState.COMPLETING
        row.claim_token = uuid4()
        row.claim_expires_at = now + timedelta(seconds=COMPLETION_LEASE_SECONDS)
        return MultipartSessionClaim(
            session=self.record(row),
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
        self._ledger.record(SESSION_RECORD_TERMINAL)
        del diagnostic_context
        if (result is None) == (failure_state is None):
            raise ValueError("exactly one of result or failure_state is required")
        row = self._row(claim.session.session_id)
        if row.state is MultipartSessionState.COMMITTED:
            if result is not None and row.terminal_result == result:
                return
            raise _state_invalid()
        if row.state not in _COMPLETION_FAMILY:
            raise _state_invalid()
        if claim.claim_token is None or row.claim_token != claim.claim_token:
            raise _in_progress()
        if row.claim_expires_at is None or row.claim_expires_at <= self._clock():
            raise _in_progress()
        if result is not None:
            row.state = MultipartSessionState.COMMITTED
            row.terminal_result = result
        else:
            assert failure_state is not None
            row.state = failure_state
            row.cleanup_state = "pending"
            row.cleanup_attempt_count = 1
            row.cleanup_next_retry_at = self._clock()
        row.claim_token = None
        row.claim_expires_at = None

    async def claim_cleanup_batch(
        self,
        *,
        batch_limit: int,
        diagnostic_context: DiagnosticContext,
    ) -> list[MultipartCleanupClaim]:
        self._ledger.record(SESSION_CLAIM_CLEANUP)
        del diagnostic_context
        if batch_limit < 1:
            raise ValueError("batch_limit must be positive")
        now = self._clock()
        for row in self.rows:
            if row.is_forward_expired(now) and row.cleanup_state == "none":
                row.state = MultipartSessionState.EXPIRED
                row.claim_token = None
                row.claim_expires_at = None
                row.cleanup_state = "pending"
                row.cleanup_attempt_count = 1
                row.cleanup_next_retry_at = now
        claims: list[MultipartCleanupClaim] = []
        for row in self.rows:
            if len(claims) >= batch_limit:
                break
            due = row.cleanup_next_retry_at is not None and row.cleanup_next_retry_at <= now
            if row.cleanup_state not in ("pending", "failed") or not due:
                continue
            row.cleanup_state = "running"
            row.claim_token = uuid4()
            row.claim_expires_at = now + timedelta(seconds=COMPLETION_LEASE_SECONDS)
            row.cleanup_next_retry_at = row.claim_expires_at
            claims.append(
                MultipartCleanupClaim(
                    session=self.record(row),
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
        self._ledger.record(SESSION_RECORD_CLEANUP)
        del diagnostic_context
        row = self._row(claim.session.session_id)
        if row.claim_token != claim.claim_token:
            raise _in_progress()
        self.cleanup_results.append(
            (row.session_id_value, is_succeeded, failure_reason.value if failure_reason else None)
        )
        if is_succeeded:
            row.state = MultipartSessionState.CLEANED
            row.cleanup_state = "succeeded"
            row.cleanup_next_retry_at = None
            row.cleanup_reason_code = None
        else:
            row.cleanup_state = "failed"
            row.cleanup_attempt_count += 1
            row.cleanup_reason_code = failure_reason.value if failure_reason else None
            row.cleanup_next_retry_at = self._clock() + timedelta(
                seconds=60 * row.cleanup_attempt_count
            )
        row.claim_token = None
        row.claim_expires_at = None


@dataclass
class FakeMultipartStagingProvider:
    """Staging-provider fake mirroring the six-method Task 4 capability."""

    ledger: CallLedger
    uploads: dict[str, str] = field(default_factory=dict)
    objects: set[str] = field(default_factory=set)
    parts: dict[str, dict[int, tuple[str, int]]] = field(default_factory=dict)
    aborted: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    create_error: Exception | None = None
    presign_error: Exception | None = None
    list_error: Exception | None = None
    complete_error: Exception | None = None
    complete_error_completes_anyway: bool = False
    abort_error: Exception | None = None
    delete_error: Exception | None = None

    def upload_part(self, staging_key: str, part_number: int, size_bytes: int) -> None:
        """Seed one provider-observed completed part of a live upload."""

        upload_id = self.uploads[staging_key]
        self.parts.setdefault(upload_id, {})[part_number] = (f"etag-{part_number}", size_bytes)

    async def create_upload(self, staging_key: str) -> MultipartProviderUploadId:
        self.ledger.record(PROVIDER_CREATE_UPLOAD)
        if self.create_error is not None:
            raise self.create_error
        upload_id = f"upload-{secrets.token_urlsafe(16)}"
        self.uploads[staging_key] = upload_id
        self.parts[upload_id] = {}
        return MultipartProviderUploadId(upload_id)

    async def presign_part(
        self,
        staging_key: str,
        upload_id: MultipartProviderUploadId,
        part_range: MultipartPartRange,
    ) -> MultipartPartUrl:
        self.ledger.record(PROVIDER_PRESIGN_PART)
        if self.presign_error is not None:
            raise self.presign_error
        return MultipartPartUrl(
            part_number=part_range.part_number,
            byte_range=part_range,
            url=f"https://staging.example.invalid/{secrets.token_urlsafe(24)}",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )

    async def list_parts(
        self, staging_key: str, upload_id: MultipartProviderUploadId
    ) -> tuple[MultipartObservedPart, ...]:
        self.ledger.record(PROVIDER_LIST_PARTS)
        if self.list_error is not None:
            raise self.list_error
        live = self.uploads.get(staging_key)
        if live != upload_id.value:
            # A completed, aborted or never-created upload addresses no parts:
            # the provider-boundary mapping of an absent upload.
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
        parts: tuple[MultipartObservedPart, ...],
    ) -> None:
        self.ledger.record(PROVIDER_COMPLETE_UPLOAD)
        if not parts:
            raise _provider_state_invalid()
        live = self.uploads.get(staging_key)
        if live != upload_id.value:
            raise _provider_state_invalid()
        if self.complete_error is not None:
            if self.complete_error_completes_anyway:
                self.objects.add(staging_key)
                del self.uploads[staging_key]
            raise self.complete_error
        self.objects.add(staging_key)
        del self.uploads[staging_key]

    async def abort_upload(self, staging_key: str, upload_id: MultipartProviderUploadId) -> None:
        self.ledger.record(PROVIDER_ABORT_UPLOAD)
        if self.abort_error is not None:
            raise self.abort_error
        if self.uploads.get(staging_key) == upload_id.value:
            del self.uploads[staging_key]
            self.parts.pop(upload_id.value, None)
        self.aborted.append((staging_key, upload_id.value))

    async def delete_staging_object(self, staging_key: str) -> None:
        self.ledger.record(PROVIDER_DELETE_STAGING)
        if self.delete_error is not None:
            raise self.delete_error
        self.objects.discard(staging_key)
        self.deleted.append(staging_key)


class FakeMultipartStagingByteSource:
    """Staging read seam serving the bytes of the session's staging object.

    The default ``digest`` carries the true preimage of the declared content
    digest; assigning a different digest serves that digest's hexadecimal text
    instead — deterministic bytes whose real SHA-256 can never match — so a
    test flips one field to model staging corruption.
    """

    def __init__(self, *, ledger: CallLedger, preimage: bytes) -> None:
        self._ledger = ledger
        self._preimages: dict[str, bytes] = {}
        self.digest = ContentDigest.parse(hashlib.sha256(preimage).hexdigest())
        self._preimages[self.digest.hexadecimal] = preimage
        self.open_error: Exception | None = None

    def _content(self) -> bytes:
        return self._preimages.get(self.digest.hexadecimal) or self.digest.hexadecimal.encode(
            "ascii"
        )

    @asynccontextmanager
    async def open_staging_stream(self, staging_key: str) -> AsyncIterator[AsyncIterable[bytes]]:
        self._ledger.record(STAGING_OPEN_STREAM)
        if self.open_error is not None:
            raise self.open_error
        yield _chunked(self._content())


def _chunked(content: bytes) -> AsyncIterator[bytes]:
    async def _iterate() -> AsyncIterator[bytes]:
        for start in range(0, len(content), _CHUNK_BYTES):
            yield content[start : start + _CHUNK_BYTES]

    return _iterate()


@dataclass
class FakeCanonicalObjectStore:
    """Object-store fake performing the real size/digest spool verification."""

    ledger: CallLedger
    clock: Callable[[], datetime]
    stored: dict[str, VerifiedObjectReceipt] = field(default_factory=dict)

    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None:
        self.ledger.record(OBJECT_STORE_RESOLVE)
        return self.stored.get(expected.content_digest.hexadecimal)

    async def store_stream(
        self,
        stream: AsyncIterable[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt:
        self.ledger.record(OBJECT_STORE_STORE_STREAM)
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
        receipt = VerifiedObjectReceipt(
            content_digest=computed,
            object_key=derive_canonical_object_key(computed),
            size_bytes=len(content),
            media_type=CanonicalMediaType.parse(media_type),
            verified_at=self.clock(),
            verification_method=VerificationMethod.UPLOADED_FULL_READ,
        )
        self.stored[computed.hexadecimal] = receipt
        return receipt

    async def verify_existing_object(self, expected: ExpectedObject) -> VerifiedObjectReceipt:
        receipt = self.stored.get(expected.content_digest.hexadecimal)
        if receipt is None:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED)
        return receipt


@dataclass
class FakeSmallFilePublicationGateway:
    """Publication-gateway fake recording the exact publish call sequence."""

    ledger: CallLedger
    calls: list[str] = field(default_factory=list)
    error: Exception | None = None

    async def publish_create(
        self,
        *,
        command: object,
        stream: object,
        policy_binding: AllowedPolicyRevisionBinding,
        bound_operation: BoundSmallFileOperation,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        del command, stream, policy_binding, bound_operation, diagnostic_context
        self.ledger.record(PUBLISH_CREATE)
        self.calls.append(PUBLISH_CREATE)
        if self.error is not None:
            raise self.error
        return _published_result()

    async def publish_update(
        self,
        *,
        command: object,
        stream: object,
        policy_binding: AllowedPolicyRevisionBinding,
        bound_operation: BoundSmallFileOperation,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        del command, stream, policy_binding, bound_operation, diagnostic_context
        self.ledger.record(PUBLISH_UPDATE)
        self.calls.append(PUBLISH_UPDATE)
        if self.error is not None:
            raise self.error
        return _published_result()


def _published_result() -> SourceVersionPublicationResult:
    return SourceVersionPublicationResult(
        source_id=uuid4(),
        source_version_id=uuid4(),
        content_version=5,
        event_id=uuid4(),
        event_sequence=11,
        content_digest=ContentDigest.parse(hashlib.sha256(b"canonical").hexdigest()),
        outcome=PublicationOutcome.PUBLISHED,
        committed_at=datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC),
    )


def stale_base_conflict() -> SourcePublicationError:
    """The existing typed stale-base conflict the publication path raises."""

    return SourcePublicationError(ErrorCode.SOURCE_VERSION_CONFLICT)


def dependency_outage() -> MultipartUploadError:
    """The typed retryable dependency outage the provider boundary raises."""

    return MultipartUploadError(ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE)


@dataclass
class MultipartServiceHarness:
    """One fully wired multipart service over the faithful in-memory fakes."""

    ledger: CallLedger
    service: MultipartUploadService
    session_store: FakeMultipartSessionStore
    evidence_store: FakeMultipartSessionEvidenceStore
    operation_store: FakeSmallFileUploadOperationStore
    staging_provider: FakeMultipartStagingProvider
    staging_reader: FakeMultipartStagingByteSource
    object_store: FakeCanonicalObjectStore
    publisher: FakeSmallFilePublicationGateway
    current_sources: FakeCanonicalSourceReadStore
    policy_guard: AllowAllSmallFilePolicyGuard | DenyingSmallFilePolicyGuard
    metrics: InMemoryMultipartUploadMetrics
    diagnostics: RecordingDiagnosticSink
    clock: MutableUtcClock
    device: SmallFileDeviceContext
    preflight: SmallFilePreflight
    session_id: MultipartUploadSessionId

    @property
    def context(self) -> DiagnosticContext:
        return build_diagnostic_context()

    def staging_key(self) -> str:
        return derive_staging_key(self.session_id)

    def row(self) -> _FakeSessionRow:
        return self.session_store._row(self.session_id)  # test seam

    async def create_ready_session(self) -> MultipartUploadSessionId:
        """Run the real create flow, bind the evidence and seed every part."""

        plan = await self.service.create_or_resume(
            preflight=self.preflight,
            device_context=self.device,
            diagnostic_context=self.context,
        )
        self.session_id = plan.session_id
        self.register_evidence()
        await self.upload_all_parts()
        return plan.session_id

    def register_evidence(self) -> None:
        """Bind the frozen bound-operation evidence of the created session."""

        row = self.row()
        operation_row = self.operation_store.rows[0]
        locator = row.preflight.normalized_locator
        is_create = row.preflight.operation is SmallFileOperation.CREATE
        bound = BoundSmallFileOperation(
            operation_id=operation_row.operation_id,
            operation_token=operation_row.operation.operation_token,
            workspace_id=row.workspace_id,
            device_id=row.device_id,
            event_id=row.preflight.event_id,
            idempotency_key=row.preflight.idempotency_key,
            operation=row.preflight.operation,
            declared_sha256=row.preflight.sha256,
            declared_size_bytes=row.preflight.size_bytes,
            declared_media_type=row.preflight.media_type,
            policy_revision_number=row.preflight.policy_revision_number,
            reserved_source_id=row.reserved_source_id,
            update_source_id=row.update_source_id,
            update_base_version_id=row.update_base_version_id,
            normalized_locator=locator if is_create else None,
            locator_fingerprint=compute_locator_fingerprint(locator) if is_create else None,
            expires_at=self.clock.now + timedelta(hours=1),
            terminal_result=None,
        )
        self.evidence_store.register(self.session_id, bound)

    async def upload_all_parts(self) -> None:
        """Seed the provider with every planned part of the live staging upload."""

        staging_key = self.staging_key()
        geometry = MultipartPartGeometry.from_size_bytes(self.preflight.size_bytes)
        for part_number in range(1, geometry.part_count + 1):
            self.staging_provider.upload_part(
                staging_key, part_number, geometry.part_range(part_number).size_bytes
            )


class RecordingDiagnosticSink:
    """Structural event-sink double recording every emitted closed event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, str]]] = []

    def emit(self, event_name: object, fields: object = None) -> None:
        name = str(getattr(event_name, "value", event_name))
        rendered = {
            str(key): str(getattr(value, "value", value)) for key, value in (fields or {}).items()
        }
        self.events.append((name, rendered))


def build_multipart_service_harness(
    *,
    size_bytes: int = DEFAULT_MULTIPART_SIZE_BYTES,
    preimage: bytes | None = None,
    denying_policy: bool = False,
    policy_system_failure: bool = False,
    stale_base: bool = False,
    missing_base: bool = False,
    diagnostics: RecordingDiagnosticSink | None = None,
) -> MultipartServiceHarness:
    """Wire the service over the fakes; the caller drives the async flows."""

    content = preimage if preimage is not None else bytes(size_bytes)
    digest = ContentDigest.parse(hashlib.sha256(content).hexdigest())
    preflight = build_update_preflight(content_digest=digest, size_bytes=size_bytes)
    device = build_device_context()
    ledger = CallLedger()
    clock = MutableUtcClock()
    session_store = FakeMultipartSessionStore(ledger, clock)
    evidence_store = FakeMultipartSessionEvidenceStore(ledger)
    operation_store = FakeSmallFileUploadOperationStore(ledger)
    staging_provider = FakeMultipartStagingProvider(ledger)
    staging_reader = FakeMultipartStagingByteSource(ledger=ledger, preimage=content)
    object_store = FakeCanonicalObjectStore(ledger, clock)
    publisher = FakeSmallFilePublicationGateway(ledger)
    if stale_base:
        current_sources = FakeCanonicalSourceReadStore(
            build_current_reference(preflight, source_version_id=uuid4())
        )
    elif missing_base:
        current_sources = FakeCanonicalSourceReadStore(None)
    else:
        current_sources = FakeCanonicalSourceReadStore(build_current_reference(preflight))
    policy_guard: AllowAllSmallFilePolicyGuard | DenyingSmallFilePolicyGuard = (
        DenyingSmallFilePolicyGuard(system_failure=policy_system_failure)
        if denying_policy or policy_system_failure
        else AllowAllSmallFilePolicyGuard()
    )
    metrics = InMemoryMultipartUploadMetrics()
    diagnostics_sink = diagnostics if diagnostics is not None else RecordingDiagnosticSink()
    service = MultipartUploadService(
        session_store=session_store,
        evidence_store=evidence_store,
        operation_store=operation_store,
        policy_guard=policy_guard,
        current_sources=current_sources,
        publication_gateway=publisher,
        object_store=object_store,
        staging_provider=staging_provider,
        staging_byte_source=staging_reader,
        metrics=metrics,
        clock=clock,
        diagnostics=diagnostics_sink,
    )
    return MultipartServiceHarness(
        ledger=ledger,
        service=service,
        session_store=session_store,
        evidence_store=evidence_store,
        operation_store=operation_store,
        staging_provider=staging_provider,
        staging_reader=staging_reader,
        object_store=object_store,
        publisher=publisher,
        current_sources=current_sources,
        policy_guard=policy_guard,
        metrics=metrics,
        clock=clock,
        diagnostics=diagnostics_sink,
        device=device,
        preflight=preflight,
        session_id=MultipartUploadSessionId(secrets.token_urlsafe(32)),
    )


def build_committed_terminal_result() -> SmallFileTerminalResult:
    """One frozen committed terminal receipt for replay assertions."""

    return SmallFileTerminalResult(
        result_kind=SmallFileTerminalResultKind.COMMITTED,
        source_id=uuid4(),
        source_version_id=uuid4(),
        content_version=5,
        committed_at=datetime(2026, 8, 28, 12, 0, 1, tzinfo=UTC),
    )


__all__ = [
    "COMPLETION_LEASE_SECONDS",
    "CURRENT_SOURCES_RESOLVE",
    "DEFAULT_MULTIPART_SIZE_BYTES",
    "EVIDENCE_LOAD",
    "OBJECT_STORE_RESOLVE",
    "OBJECT_STORE_STORE_STREAM",
    "OPERATION_RESERVE",
    "POLICY_GUARD",
    "PROVIDER_ABORT_UPLOAD",
    "PROVIDER_COMPLETE_UPLOAD",
    "PROVIDER_CREATE_UPLOAD",
    "PROVIDER_DELETE_STAGING",
    "PROVIDER_LIST_PARTS",
    "PROVIDER_PRESIGN_PART",
    "PUBLISH_CREATE",
    "PUBLISH_UPDATE",
    "SESSION_CLAIM_CLEANUP",
    "SESSION_CLAIM_COMPLETION",
    "SESSION_LOAD",
    "SESSION_RECORD_CLEANUP",
    "SESSION_RECORD_IDENTITY",
    "SESSION_RECORD_PART",
    "SESSION_RECORD_TERMINAL",
    "SESSION_RESERVE",
    "STAGING_OPEN_STREAM",
    "AllowAllSmallFilePolicyGuard",
    "CallLedger",
    "DenyingSmallFilePolicyGuard",
    "FakeCanonicalObjectStore",
    "FakeCanonicalSourceReadStore",
    "FakeMultipartSessionEvidenceStore",
    "FakeMultipartSessionStore",
    "FakeMultipartStagingByteSource",
    "FakeMultipartStagingProvider",
    "FakeSmallFilePublicationGateway",
    "FakeSmallFileUploadOperationStore",
    "MultipartServiceHarness",
    "MutableUtcClock",
    "build_committed_terminal_result",
    "build_current_reference",
    "build_device_context",
    "build_diagnostic_context",
    "build_multipart_service_harness",
    "build_update_preflight",
    "dependency_outage",
    "stale_base_conflict",
]
