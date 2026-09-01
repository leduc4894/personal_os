"""Narrow in-memory fakes proving the small-file sync service orchestration.

Every fake records the exact port call sequence into one shared ledger so a
test can assert the full cross-port order (policy preflight, replay lookup,
reservation, current-base resolution, spool/verification, publication, the
terminal write) with closed string entries only. The operation-store fake
mirrors the durable PostgreSQL semantics of task 6: identity-keyed rows,
token rotation on pending re-preflight (a stale token surfaces as the closed
not-found error), payload-fingerprint matching, expiry that ends an unclaimed
reservation but not a claimed receive or terminal evidence, and an idempotent
guarded terminal transition.
The object-store fake performs the real size/digest verification over the
streamed bytes and models content-addressable dedup so the publication
service's resolve-first path hits. The publication stack runs the REAL
:class:`~personal_os.sources.publication.SourceVersionPublicationService`
over these fakes, so the orchestration tests prove genuine end-to-end
ordering, replay and single-publication behavior. No fake retains or echoes
locator, digest, token or payload sentinels in ledgers or assertions.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.enforcement import AllowedPolicyRevisionBinding
from personal_os.exclusion_policy.errors import ExclusionPolicyError
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
    NormalizedLocator,
    SmallFileConflictCaptureResult,
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
from personal_os.small_file_sync.metrics import InMemorySmallFileSyncMetrics
from personal_os.small_file_sync.ports import SmallFileBoundOperation
from personal_os.small_file_sync.service import SmallFilePreflightResult, SmallFileSyncService
from personal_os.sources.commands import SourceType
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.fingerprint import RequestFingerprint, SourceVersionCommand
from personal_os.sources.metrics import InMemorySourcePublicationMetrics
from personal_os.sources.publication import SourceVersionPublicationService
from personal_os.sources.reading import (
    CanonicalReadStateError,
    CanonicalSourceReference,
    ReadCurrentSourceCommand,
)
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult

#: Shared ledger entry constants: one closed string per observed port call.
SYNC_POLICY_GUARD: Final[str] = "sync_policy_guard.authorize_small_file"
STORE_RESOLVE_TERMINAL: Final[str] = "operation_store.resolve_terminal_result"
STORE_RESERVE_OPERATION: Final[str] = "operation_store.reserve_operation"
STORE_RECORD_TERMINAL: Final[str] = "operation_store.record_terminal_result"
STORE_RESOLVE_BOUND: Final[str] = "operation_store.resolve_bound_operation"
STORE_RECORD_BOUND_TERMINAL: Final[str] = "operation_store.record_bound_terminal_result"
STORE_RECORD_BOUND_TERMINAL_FAILURE: Final[str] = "operation_store.record_bound_terminal_failure"
OBJECT_STORE_RESOLVE: Final[str] = "object_store.resolve_verified_object"
OBJECT_STORE_STORE_STREAM: Final[str] = "object_store.store_stream"
CURRENT_SOURCES_RESOLVE: Final[str] = "current_sources.resolve_current"
PUBLICATION_GUARD: Final[str] = "publication.policy_guard.authorize_publication"
PUBLICATION_RESOLVE_COMMITTED: Final[str] = "publication_store.resolve_committed"
PUBLICATION_COMMIT_CREATE: Final[str] = "publication_store.commit_create"
PUBLICATION_COMMIT_UPDATE: Final[str] = "publication_store.commit_update"
CONFLICT_CAPTURE_GATEWAY_CAPTURE: Final[str] = "conflict_capture.capture_stale_update"
CONFLICT_CAPTURE_GATEWAY_RESOLVE: Final[str] = "conflict_capture.resolve_captured_conflict"

#: Fixed canonical content and its digest shared by the builders.
SYNC_CONTENT_BYTES: Final[bytes] = b"small-file canonical bytes for the sync service fakes\n"
SYNC_CONTENT_DIGEST: Final[ContentDigest] = ContentDigest.parse(
    hashlib.sha256(SYNC_CONTENT_BYTES).hexdigest()
)
SYNC_MEDIA_TYPE: Final[CanonicalMediaType] = CanonicalMediaType.parse("text/markdown")
_CURRENT_BASE_COMMITTED_AT: Final[datetime] = datetime(2026, 8, 18, 9, 30, 0, tzinfo=UTC)

_PENDING_STATE: Final[str] = "pending"
_RECEIVING_STATE: Final[str] = "receiving"
_COMMITTED_STATE: Final[str] = "committed"
_FAILED_STATE: Final[str] = "failed"
_EXPIRY_SECONDS: Final[int] = 900


@dataclass
class CallLedger:
    """Append-only shared record of observed port calls across all fakes."""

    entries: list[str] = field(default_factory=list)

    def record(self, entry: str) -> None:
        self.entries.append(entry)

    def count(self, entry: str) -> int:
        return self.entries.count(entry)


@dataclass
class FixedUtcClock:
    """Injectable aware UTC clock returning one fixed moment."""

    moment: datetime

    def __call__(self) -> datetime:
        return self.moment


class ProbedByteStream:
    """Caller-owned async byte stream that reports whether it was consumed."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._remaining = list(chunks)
        self.was_consumed = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        if not self._remaining:
            raise StopAsyncIteration
        self.was_consumed = True
        return self._remaining.pop(0)


def build_diagnostic_context() -> DiagnosticContext:
    """A fresh server-owned diagnostic context for one request-bound unit of work."""

    return create_diagnostic_context().context


def build_device_context() -> SmallFileDeviceContext:
    """A fresh credential-derived device context."""

    return SmallFileDeviceContext(device_id=uuid4(), workspace_id=uuid4())


def build_create_preflight(
    *,
    content: bytes = SYNC_CONTENT_BYTES,
    event_id: UUID | None = None,
    idempotency_key: SmallFileIdempotencyKey | None = None,
    policy_revision_number: int = 7,
) -> SmallFilePreflight:
    """A valid create preflight whose declared fingerprint covers ``content``."""

    return SmallFilePreflight(
        event_id=event_id if event_id is not None else uuid4(),
        idempotency_key=idempotency_key
        if idempotency_key is not None
        else SmallFileIdempotencyKey(str(uuid4())),
        operation=SmallFileOperation.CREATE,
        local_file_id=uuid4(),
        source_id=None,
        base_version_id=None,
        normalized_locator=NormalizedLocator("notes/synced-note.md"),
        sha256=ContentDigest.parse(hashlib.sha256(content).hexdigest()),
        size_bytes=len(content),
        media_type=SYNC_MEDIA_TYPE,
        policy_revision_number=policy_revision_number,
    )


def build_update_preflight(
    *,
    source_id: UUID,
    base_version_id: UUID,
    content: bytes = SYNC_CONTENT_BYTES,
) -> SmallFilePreflight:
    """A valid update preflight over the given target source and base version."""

    return SmallFilePreflight(
        event_id=uuid4(),
        idempotency_key=SmallFileIdempotencyKey(str(uuid4())),
        operation=SmallFileOperation.UPDATE,
        local_file_id=uuid4(),
        source_id=source_id,
        base_version_id=base_version_id,
        normalized_locator=NormalizedLocator("notes/synced-note.md"),
        sha256=ContentDigest.parse(hashlib.sha256(content).hexdigest()),
        size_bytes=len(content),
        media_type=SYNC_MEDIA_TYPE,
        policy_revision_number=7,
    )


def build_current_reference(
    preflight: SmallFilePreflight,
    *,
    source_version_id: UUID | None = None,
    content_digest: ContentDigest | None = None,
) -> CanonicalSourceReference:
    """The current version of the update target, with overridable identity.

    By default the current version IS the preflight's declared base carrying
    the declared digest — the no-change shape. Override ``source_version_id``
    to model a stale base and ``content_digest`` to model changed content.
    """

    update_source_id = preflight.source_id if preflight.source_id is not None else uuid4()
    return CanonicalSourceReference(
        workspace_id=uuid4(),
        source_id=update_source_id,
        source_version_id=source_version_id
        if source_version_id is not None
        else (preflight.base_version_id if preflight.base_version_id is not None else uuid4()),
        content_version=3,
        source_type=SourceType.MARKDOWN,
        expected_object=ExpectedObject(
            content_digest=content_digest if content_digest is not None else preflight.sha256,
            size_bytes=preflight.size_bytes,
            media_type=preflight.media_type,
        ),
        committed_at=_CURRENT_BASE_COMMITTED_AT,
    )


@dataclass
class _OperationRecord:
    """One durable operation row as the fake store keeps it."""

    operation_id: UUID
    operation_token: UploadOperationToken
    preflight: SmallFilePreflight
    device_context: SmallFileDeviceContext
    reserved_source_id: UUID | None
    expires_at: datetime
    state: str
    policy_revision_number: int
    terminal_result: SmallFileTerminalResult | None = None
    safe_error_code: str | None = None


@dataclass
class FakeSmallFileUploadOperationStore:
    """Operation-store fake mirroring the durable adapter semantics of task 6.

    Rows are keyed by the credential-derived identity quadruple; the payload
    fingerprint admits no substitution. A receive claims its row by flipping
    it to ``receiving``; a same-identity re-preflight may re-reserve only an
    expired pending row, never a claimed receive. The receive claim and its
    guarded terminal transition retain the token/revision fence across the
    reservation deadline and refuse every other state.
    """

    ledger: CallLedger
    clock: Callable[[], datetime]
    expiry_seconds: int = _EXPIRY_SECONDS
    now_override: datetime | None = None
    declared_size_override_bytes: int | None = None
    bound_workspace_id_override: UUID | None = None
    records: list[_OperationRecord] = field(default_factory=list)

    def _now(self) -> datetime:
        return self.now_override if self.now_override is not None else datetime.now(UTC)

    def _identity(
        self, preflight: SmallFilePreflight, device_context: SmallFileDeviceContext
    ) -> tuple[UUID, UUID, UUID, str]:
        return (
            device_context.workspace_id,
            device_context.device_id,
            preflight.event_id,
            preflight.idempotency_key.value,
        )

    def _identity_record(
        self, preflight: SmallFilePreflight, device_context: SmallFileDeviceContext
    ) -> _OperationRecord | None:
        identity = self._identity(preflight, device_context)
        for record in self.records:
            if self._identity(record.preflight, record.device_context) == identity:
                return record
        return None

    def _token_record(self, operation_token: UploadOperationToken) -> _OperationRecord | None:
        for record in self.records:
            if record.operation_token.value == operation_token.value:
                return record
        return None

    def _fingerprint_matches(self, record: _OperationRecord, preflight: SmallFilePreflight) -> bool:
        return (
            record.preflight.operation is preflight.operation
            and record.preflight.sha256 == preflight.sha256
            and record.preflight.size_bytes == preflight.size_bytes
            and record.preflight.media_type == preflight.media_type
            and record.preflight.source_id == preflight.source_id
            and record.preflight.base_version_id == preflight.base_version_id
        )

    def record_for_token(self, operation_token: UploadOperationToken) -> _OperationRecord | None:
        """Test introspection: the durable record behind one opaque token."""

        return self._token_record(operation_token)

    async def resolve_terminal_result(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileTerminalResult | None:
        del diagnostic_context
        self.ledger.record(STORE_RESOLVE_TERMINAL)
        record = self._identity_record(preflight, device_context)
        if record is None:
            return None
        if not self._fingerprint_matches(record, preflight):
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH)
        return record.terminal_result

    async def reserve_operation(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        policy_binding: AllowedPolicyRevisionBinding,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileUploadOperation:
        del diagnostic_context
        self.ledger.record(STORE_RESERVE_OPERATION)
        if policy_binding.workspace_id != device_context.workspace_id:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        record = self._identity_record(preflight, device_context)
        if record is None:
            record = _OperationRecord(
                operation_id=uuid4(),
                operation_token=UploadOperationToken(secrets.token_urlsafe(32)),
                preflight=preflight,
                device_context=device_context,
                reserved_source_id=(
                    uuid4() if preflight.operation is SmallFileOperation.CREATE else None
                ),
                expires_at=self._now() + timedelta(seconds=self.expiry_seconds),
                state=_PENDING_STATE,
                policy_revision_number=policy_binding.policy_revision_number,
            )
            self.records.append(record)
        else:
            if not self._fingerprint_matches(record, preflight):
                raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH)
            if record.state == _COMMITTED_STATE:
                raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
            if record.state == _RECEIVING_STATE:
                record.policy_revision_number = policy_binding.policy_revision_number
                raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
            # Mirroring the durable adapter: only an expired pending record
            # re-reserves with a fresh token and an extended deadline.
            if record.expires_at <= self._now():
                record.expires_at = self._now() + timedelta(seconds=self.expiry_seconds)
            record.operation_token = UploadOperationToken(secrets.token_urlsafe(32))
            record.state = _PENDING_STATE
            record.policy_revision_number = policy_binding.policy_revision_number
        return SmallFileUploadOperation(
            operation_token=record.operation_token,
            preflight=preflight,
            device_context=device_context,
            reserved_source_id=record.reserved_source_id,
            expires_at=record.expires_at,
        )

    async def record_terminal_result(
        self,
        operation: SmallFileUploadOperation,
        result: SmallFileTerminalResult,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del diagnostic_context
        self.ledger.record(STORE_RECORD_TERMINAL)
        record = self._token_record(operation.operation_token)
        if record is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND)
        self._apply_terminal_transition(
            record,
            result,
            operation.device_context,
            require_pending=True,
        )

    async def resolve_bound_operation(
        self,
        operation_token: UploadOperationToken,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileBoundOperation:
        del diagnostic_context
        self.ledger.record(STORE_RESOLVE_BOUND)
        record = self._token_record(operation_token)
        if record is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND)
        if (
            record.device_context.workspace_id != device_context.workspace_id
            or record.device_context.device_id != device_context.device_id
        ):
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH)
        if record.state == _PENDING_STATE:
            if record.expires_at <= self._now():
                raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_EXPIRED)
            record.state = _RECEIVING_STATE
        return self._bound_operation(record)

    async def record_bound_terminal_result(
        self,
        bound: SmallFileBoundOperation,
        result: SmallFileTerminalResult,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del diagnostic_context
        self.ledger.record(STORE_RECORD_BOUND_TERMINAL)
        record = self._token_record(bound.operation_token)
        if record is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND)
        if self._bound_operation(record) != bound:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH)
        self._apply_terminal_transition(
            record,
            result,
            SmallFileDeviceContext(device_id=bound.device_id, workspace_id=bound.workspace_id),
            require_claimed=True,
        )

    async def record_bound_terminal_failure(
        self,
        bound: SmallFileBoundOperation,
        error_code: ErrorCode,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        """Guarded ``receiving -> failed`` transition mirroring the durable adapter.

        Only the identical bound/code pair replays idempotently; a drifted
        binding, a different closed token, a committed row or an unclaimed
        row fail closed with the adapter's own closed errors.
        """
        del diagnostic_context
        self.ledger.record(STORE_RECORD_BOUND_TERMINAL_FAILURE)
        record = self._token_record(bound.operation_token)
        if record is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND)
        if (
            record.device_context.workspace_id != bound.workspace_id
            or record.device_context.device_id != bound.device_id
        ):
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH)
        if record.state == _FAILED_STATE:
            if record.safe_error_code == error_code.value:
                return
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        if record.state != _RECEIVING_STATE:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        record.state = _FAILED_STATE
        record.safe_error_code = error_code.value

    def _apply_terminal_transition(
        self,
        record: _OperationRecord,
        result: SmallFileTerminalResult,
        device_context: SmallFileDeviceContext,
        *,
        require_claimed: bool = False,
        require_pending: bool = False,
    ) -> None:
        if (
            record.device_context.workspace_id != device_context.workspace_id
            or record.device_context.device_id != device_context.device_id
        ):
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH)
        if record.state == _COMMITTED_STATE:
            if record.terminal_result == result:
                return
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        if require_pending and record.state != _PENDING_STATE:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        if require_claimed and record.state != _RECEIVING_STATE:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        if not require_claimed and record.expires_at <= self._now():
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_EXPIRED)
        record.state = _COMMITTED_STATE
        record.terminal_result = result

    def _bound_operation(self, record: _OperationRecord) -> SmallFileBoundOperation:
        normalized_locator: NormalizedLocator | None = None
        locator_fingerprint: str | None = None
        if record.preflight.operation is SmallFileOperation.CREATE:
            normalized_locator = record.preflight.normalized_locator
            locator_fingerprint = compute_locator_fingerprint(record.preflight.normalized_locator)
        return SmallFileBoundOperation(
            operation_id=record.operation_id,
            operation_token=record.operation_token,
            workspace_id=(
                self.bound_workspace_id_override
                if self.bound_workspace_id_override is not None
                else record.device_context.workspace_id
            ),
            device_id=record.device_context.device_id,
            event_id=record.preflight.event_id,
            idempotency_key=record.preflight.idempotency_key,
            operation=record.preflight.operation,
            declared_sha256=record.preflight.sha256,
            declared_size_bytes=(
                self.declared_size_override_bytes
                if self.declared_size_override_bytes is not None
                else record.preflight.size_bytes
            ),
            declared_media_type=record.preflight.media_type,
            policy_revision_number=record.policy_revision_number,
            reserved_source_id=record.reserved_source_id,
            update_source_id=record.preflight.source_id,
            update_base_version_id=record.preflight.base_version_id,
            normalized_locator=normalized_locator,
            locator_fingerprint=locator_fingerprint,
            expires_at=record.expires_at,
            terminal_result=record.terminal_result,
        )


@dataclass
class FakeCanonicalObjectStore:
    """Object-store fake verifying real size/digest over the streamed bytes.

    ``store_stream`` computes the true size and SHA-256 of the consumed bytes
    and raises the typed input-invalid error with the closed size or digest
    reason token on any mismatch — exactly the bounded verification contract
    the receive path depends on. Stored digests become content-addressable:
    ``resolve_verified_object`` returns a receipt for them so the publication
    service's resolve-first path hits and never consumes a fallback stream.
    """

    ledger: CallLedger
    clock: Callable[[], datetime]
    known_digests: set[str] = field(default_factory=set)
    store_stream_calls: list[tuple[int, str, str | None]] = field(default_factory=list)

    def _receipt(self, expected: ExpectedObject) -> VerifiedObjectReceipt:
        return VerifiedObjectReceipt(
            content_digest=expected.content_digest,
            object_key=derive_canonical_object_key(expected.content_digest),
            size_bytes=expected.size_bytes,
            media_type=expected.media_type,
            verified_at=self.clock(),
            verification_method=VerificationMethod.UPLOADED_FULL_READ,
        )

    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None:
        self.ledger.record(OBJECT_STORE_RESOLVE)
        if expected.content_digest.hexadecimal in self.known_digests:
            return self._receipt(expected)
        return None

    async def store_stream(
        self,
        stream: AsyncIterator[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt:
        self.ledger.record(OBJECT_STORE_STORE_STREAM)
        content = b"".join([chunk async for chunk in stream])
        self.store_stream_calls.append((expected_size_bytes, media_type, claimed_sha256))
        if len(content) != expected_size_bytes:
            raise ObjectStorageError(
                ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
                safe_details={"reason": SIZE_MISMATCH},
            )
        computed_digest = ContentDigest.parse(hashlib.sha256(content).hexdigest())
        if claimed_sha256 is not None and computed_digest.hexadecimal != claimed_sha256:
            raise ObjectStorageError(
                ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
                safe_details={"reason": DIGEST_MISMATCH},
            )
        self.known_digests.add(computed_digest.hexadecimal)
        return self._receipt(
            ExpectedObject(
                content_digest=computed_digest,
                size_bytes=len(content),
                media_type=CanonicalMediaType.parse(media_type),
            )
        )


@dataclass
class FakeCurrentSourceStore:
    """Current-source resolver fake recording every resolve call.

    ``reference`` is the resolved current version of the update target, or
    ``None`` to model a source whose canonical current pointer is missing
    (the read store raises the typed read-state error). ``resolve_error``
    models the durable read boundary's locked policy recheck raising one
    typed exclusion-policy failure while resolving the current source.
    """

    ledger: CallLedger
    reference: CanonicalSourceReference | None
    resolve_calls: list[UUID] = field(default_factory=list)
    resolve_error: Exception | None = None

    async def resolve_current(
        self, command: ReadCurrentSourceCommand, diagnostic_context: DiagnosticContext
    ) -> CanonicalSourceReference:
        del diagnostic_context
        self.ledger.record(CURRENT_SOURCES_RESOLVE)
        self.resolve_calls.append(command.source_id)
        if self.resolve_error is not None:
            raise self.resolve_error
        if self.reference is None:
            raise CanonicalReadStateError(source_id=command.source_id)
        return self.reference


@dataclass
class FakeConflictCaptureGateway:
    """Conflict-capture gateway fake replaying by event identity.

    Mirrors the real gateway's contract: the first capture of one
    (workspace, event) identity mints one frozen receipt; an exact replay of
    that identity returns the stored receipt unchanged; the membership
    lookup answers preflight replay. Only the opaque receipt crosses back —
    no bytes, digest or locator are retained or echoed.
    """

    ledger: CallLedger
    clock: Callable[[], datetime]
    capture_calls: int = 0
    resolve_calls: int = 0
    receipts: dict[tuple[UUID, UUID], SmallFileConflictCaptureResult] = field(default_factory=dict)

    async def capture_stale_update(
        self,
        *,
        bound_operation: SmallFileBoundOperation,
        verified_candidate: VerifiedObjectReceipt,
        observed_remote_version_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileConflictCaptureResult:
        del verified_candidate, diagnostic_context
        self.ledger.record(CONFLICT_CAPTURE_GATEWAY_CAPTURE)
        self.capture_calls += 1
        identity = (bound_operation.workspace_id, bound_operation.event_id)
        stored = self.receipts.get(identity)
        if stored is not None:
            return stored
        update_source_id = bound_operation.update_source_id
        if update_source_id is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        receipt = SmallFileConflictCaptureResult(
            conflict_id=uuid4(),
            source_id=update_source_id,
            observed_remote_version_id=observed_remote_version_id,
            captured_at=self.clock(),
        )
        self.receipts[identity] = receipt
        return receipt

    async def resolve_captured_conflict(
        self,
        *,
        workspace_id: UUID,
        originating_event_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileConflictCaptureResult | None:
        del diagnostic_context
        self.ledger.record(CONFLICT_CAPTURE_GATEWAY_RESOLVE)
        self.resolve_calls += 1
        return self.receipts.get((workspace_id, originating_event_id))

    @property
    def capture_count(self) -> int:
        """The number of distinct captured conflicts (replays excluded)."""

        return len(self.receipts)


@dataclass
class AllowingSmallFilePolicyGuard:
    """Small-file policy-guard fake recording the boundary call and allowing."""

    ledger: CallLedger
    policy_revision_number: int = 1
    authorize_calls: list[UUID] = field(default_factory=list)

    async def authorize_small_file(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> AllowedPolicyRevisionBinding:
        del diagnostic_context
        self.ledger.record(SYNC_POLICY_GUARD)
        self.authorize_calls.append(preflight.event_id)
        return AllowedPolicyRevisionBinding(
            workspace_id=device_context.workspace_id,
            policy_revision_number=self.policy_revision_number,
        )


@dataclass
class DenyingSmallFilePolicyGuard:
    """Small-file policy-guard fake raising the typed denial."""

    error: Exception

    async def authorize_small_file(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> AllowedPolicyRevisionBinding:
        del preflight, device_context, diagnostic_context
        raise self.error


def denying_small_file_policy_guard() -> DenyingSmallFilePolicyGuard:
    """Build the guard fake raising one typed exclusion-policy denial."""

    return DenyingSmallFilePolicyGuard(
        error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)
    )


@dataclass
class FakePublicationPolicyGuard:
    """Publication-boundary guard fake for the real publication service."""

    ledger: CallLedger
    error: Exception | None = None

    async def authorize_publication(
        self,
        command: SourceVersionCommand,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del command, diagnostic_context
        self.ledger.record(PUBLICATION_GUARD)
        if self.error is not None:
            raise self.error


@dataclass
class FakeSmallFilePublicationGateway:
    """Gateway fake that records each immutable invocation binding.

    The wrapped real publication orchestrator preserves the existing
    publication-store behavior, while the optional per-revision barriers let
    concurrency tests prove one receive cannot overwrite another receive's
    authorization evidence.
    """

    publication_service: SourceVersionPublicationService
    bindings: list[AllowedPolicyRevisionBinding] = field(default_factory=list)
    entered_by_revision: dict[int, asyncio.Event] = field(default_factory=dict)
    release_by_revision: dict[int, asyncio.Event] = field(default_factory=dict)

    async def publish_create(
        self,
        *,
        command: SourceVersionCommand,
        stream: AsyncIterable[bytes],
        policy_binding: AllowedPolicyRevisionBinding,
        bound_operation: SmallFileBoundOperation,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        del bound_operation
        await self._hold_binding(policy_binding)
        return await self.publication_service.publish_create(
            command=command,
            stream=stream,
            diagnostic_context=diagnostic_context,
        )

    async def publish_update(
        self,
        *,
        command: SourceVersionCommand,
        stream: AsyncIterable[bytes],
        policy_binding: AllowedPolicyRevisionBinding,
        bound_operation: SmallFileBoundOperation,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        del bound_operation
        await self._hold_binding(policy_binding)
        return await self.publication_service.publish_update(
            command=command,
            stream=stream,
            diagnostic_context=diagnostic_context,
        )

    async def _hold_binding(self, policy_binding: AllowedPolicyRevisionBinding) -> None:
        self.bindings.append(policy_binding)
        revision_number = policy_binding.policy_revision_number
        entered = self.entered_by_revision.get(revision_number)
        if entered is not None:
            entered.set()
        release = self.release_by_revision.get(revision_number)
        if release is not None:
            await release.wait()


@dataclass
class FakeSourcePublicationStore:
    """Publication-store fake modelling lock-guarded idempotent commits.

    ``resolve_committed`` and the commit methods share one identity map, and a
    commit whose identity is already committed returns the frozen result —
    the under-lock idempotency recheck the durable adapter performs — so
    concurrent receives converge on exactly one canonical publication. A
    create commit inserts the command's source id into ``source_rows`` so a
    test can prove reservation alone never inserted a ``sources`` row.
    """

    ledger: CallLedger
    update_outcome: PublicationOutcome = PublicationOutcome.PUBLISHED
    source_rows: set[UUID] = field(default_factory=set)
    commit_invocations: int = 0
    committed_fingerprints: list[RequestFingerprint] = field(default_factory=list)
    _committed_by_identity: dict[tuple[UUID, UUID, UUID], RequestFingerprint] = field(
        default_factory=dict
    )
    _results: dict[RequestFingerprint, SourceVersionPublicationResult] = field(default_factory=dict)

    def _identity(self, command: SourceVersionCommand) -> tuple[UUID, UUID, UUID]:
        return (command.workspace_id, command.source_id, command.event_id)

    async def resolve_committed(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult | None:
        del diagnostic_context
        self.ledger.record(PUBLICATION_RESOLVE_COMMITTED)
        committed_fingerprint = self._committed_by_identity.get(self._identity(command))
        if committed_fingerprint is None:
            return None
        if committed_fingerprint != request_fingerprint:
            raise SourcePublicationError(ErrorCode.SOURCE_IDEMPOTENCY_MISMATCH)
        return self._results[committed_fingerprint]

    async def commit_create(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        *,
        preflight_decision: object = None,
    ) -> SourceVersionPublicationResult:
        del receipt, preflight_decision
        self.ledger.record(PUBLICATION_COMMIT_CREATE)
        return self._commit(command, request_fingerprint, diagnostic_context, is_create=True)

    async def commit_update(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        *,
        preflight_decision: object = None,
    ) -> SourceVersionPublicationResult:
        del receipt, preflight_decision
        self.ledger.record(PUBLICATION_COMMIT_UPDATE)
        return self._commit(command, request_fingerprint, diagnostic_context, is_create=False)

    def _commit(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        diagnostic_context: DiagnosticContext,
        *,
        is_create: bool,
    ) -> SourceVersionPublicationResult:
        del diagnostic_context
        self.commit_invocations += 1
        identity = self._identity(command)
        committed_fingerprint = self._committed_by_identity.get(identity)
        if committed_fingerprint is not None:
            if committed_fingerprint != request_fingerprint:
                raise SourcePublicationError(ErrorCode.SOURCE_IDEMPOTENCY_MISMATCH)
            return self._results[committed_fingerprint]
        result = SourceVersionPublicationResult(
            source_id=command.source_id,
            source_version_id=uuid4(),
            content_version=1 if is_create else 2,
            event_id=command.event_id,
            event_sequence=1,
            content_digest=command.expected_object.content_digest,
            outcome=PublicationOutcome.PUBLISHED if is_create else self.update_outcome,
            committed_at=datetime.now(UTC),
        )
        if is_create:
            self.source_rows.add(command.source_id)
        self.committed_fingerprints.append(request_fingerprint)
        self._committed_by_identity[identity] = request_fingerprint
        self._results[request_fingerprint] = result
        return result


type SmallFilePolicyGuardFake = AllowingSmallFilePolicyGuard | DenyingSmallFilePolicyGuard


@dataclass
class ServiceHarness:
    """The real services wired over the recording fakes and one shared ledger."""

    service: SmallFileSyncService
    operation_store: FakeSmallFileUploadOperationStore
    object_store: FakeCanonicalObjectStore
    publication_store: FakeSourcePublicationStore
    publication_gateway: FakeSmallFilePublicationGateway
    current_sources: FakeCurrentSourceStore
    policy_guard: SmallFilePolicyGuardFake
    conflict_capture: FakeConflictCaptureGateway
    metrics: InMemorySmallFileSyncMetrics
    ledger: CallLedger
    clock: FixedUtcClock
    stale_update_preflight: SmallFilePreflight | None = None
    stale_update_device: SmallFileDeviceContext | None = None
    stale_update_token: UploadOperationToken | None = None

    @property
    def publication_count(self) -> int:
        """The number of canonical publication commits observed."""

        return self.publication_store.commit_invocations

    async def receive_stale_update(self) -> SmallFileConflictCaptureResult:
        """Run one stale-base update preflight and upload its verified candidate.

        Seeds the current pointer on a different version than the declared
        base (the stale shape), runs the preflight — which must answer the
        ``conflict`` outcome together with the capture operation grant — and
        streams the declared content through the conflict-candidate receive
        path. The event identity, device context and granted token are kept
        for :meth:`replay_same_event`.
        """

        device_context = build_device_context()
        preflight = build_update_preflight(source_id=uuid4(), base_version_id=uuid4())
        self.current_sources.reference = build_current_reference(
            preflight, source_version_id=uuid4(), content_digest=ContentDigest.parse("c" * 64)
        )
        granted = await self.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        token = granted.operation_token
        if token is None:
            raise AssertionError("the stale-update preflight must grant a capture operation")
        self.stale_update_preflight = preflight
        self.stale_update_device = device_context
        self.stale_update_token = token
        return await self.service.receive_conflict_candidate(
            operation_token=token,
            device_context=device_context,
            stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
            diagnostic_context=build_diagnostic_context(),
        )

    async def replay_same_event(self) -> SmallFilePreflightResult:
        """Re-preflight the captured event identity; the same conflict answers."""

        preflight = self.stale_update_preflight
        device_context = self.stale_update_device
        if preflight is None or device_context is None:
            raise AssertionError("receive_stale_update must run before replay_same_event")
        return await self.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )


def build_service_harness(
    *,
    current_reference: CanonicalSourceReference | None = None,
    denying_policy_guard: bool = False,
    policy_guard_error: Exception | None = None,
    publication_guard_error: Exception | None = None,
    update_outcome: PublicationOutcome = PublicationOutcome.PUBLISHED,
) -> ServiceHarness:
    """Wire the real services over the recording fakes and one shared ledger."""

    ledger = CallLedger()
    clock = FixedUtcClock(moment=datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC))
    object_store = FakeCanonicalObjectStore(ledger=ledger, clock=clock)
    publication_store = FakeSourcePublicationStore(ledger=ledger, update_outcome=update_outcome)
    publication_service = SourceVersionPublicationService(
        store=publication_store,
        object_store=object_store,
        metrics=InMemorySourcePublicationMetrics(),
        clock=clock,
        policy_guard=FakePublicationPolicyGuard(ledger=ledger, error=publication_guard_error),
    )
    publication_gateway = FakeSmallFilePublicationGateway(publication_service=publication_service)
    operation_store = FakeSmallFileUploadOperationStore(ledger=ledger, clock=clock)
    current_sources = FakeCurrentSourceStore(ledger=ledger, reference=current_reference)
    conflict_capture = FakeConflictCaptureGateway(ledger=ledger, clock=clock)
    metrics = InMemorySmallFileSyncMetrics()
    policy_guard: SmallFilePolicyGuardFake
    if policy_guard_error is not None:
        policy_guard = DenyingSmallFilePolicyGuard(error=policy_guard_error)
    elif denying_policy_guard:
        policy_guard = denying_small_file_policy_guard()
    else:
        policy_guard = AllowingSmallFilePolicyGuard(ledger=ledger)
    service = SmallFileSyncService(
        operation_store=operation_store,
        policy_guard=policy_guard,
        publication_gateway=publication_gateway,
        object_store=object_store,
        current_sources=current_sources,
        conflict_capture=conflict_capture,
        metrics=metrics,
        clock=clock,
    )
    return ServiceHarness(
        service=service,
        operation_store=operation_store,
        object_store=object_store,
        publication_store=publication_store,
        publication_gateway=publication_gateway,
        current_sources=current_sources,
        policy_guard=policy_guard,
        conflict_capture=conflict_capture,
        metrics=metrics,
        ledger=ledger,
        clock=clock,
    )
