"""Composition of the small-file sync runtime: the serve graph and its offline double.

:func:`compose_small_file_sync` builds the real serve graph the API process
runs: the durable PostgreSQL upload-operation store, the real R2
content-addressable object store behind a lazy per-process client (no
connection opens at composition — the first store call does), the real
:class:`~personal_os.exclusion_policy.enforcement.PolicyEnforcementService`
behind the locator-aware :class:`PolicyEnforcementSmallFileGuard` and as an
invocation-local publication gateway guard, the durable source-publication store and
the canonical read store over the shared engine, and the in-memory low
cardinality metrics sinks.

:func:`compose_offline_small_file_sync` builds the deterministic offline
graph used by the OpenAPI export and by route tests: identity-keyed in-memory
operation rows mirroring the durable adapter semantics (token rotation on
re-preflight, payload-fingerprint matching, expiry that ends continuation but
never terminal evidence, an idempotent guarded terminal transition), an
object-store double performing the real size/digest verification over the
streamed bytes, an invocation-local publication gateway over an in-memory
idempotent publication store, and the real
:class:`~personal_os.small_file_sync.service.SmallFileSyncService` binding
them together with the in-memory metrics sink and a state-owned clock. It
reads no environment value, no secret file, no database and no provider
client, so the offline contract document stays byte-deterministic while
route tests seed behavior through the public knobs of
:class:`OfflineSmallFileSyncState` (policy denial, the current update base,
the frozen clock) and observe safety through its public counters.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from api_runtime.exclusion_policy_crypto import Ed25519PolicySigner, Ed25519PolicyVerifier
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.logging import DiagnosticLogger
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.exclusion_policy.enforcement import (
    AllowedPolicyRevisionBinding,
    KeyedTrustAnchorVerifier,
    PolicyEnforcementService,
    PublicationPolicyEvidence,
    default_utc_clock,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.metrics import PolicyBoundary
from personal_os.object_storage import (
    CanonicalMediaType,
    CanonicalObjectKey,
    CanonicalObjectStore,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from personal_os.object_storage.errors import DIGEST_MISMATCH, SIZE_MISMATCH, ObjectStorageError
from personal_os.small_file_sync.contracts import (
    SmallFileDeviceContext,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFileTerminalResult,
    SmallFileUploadOperation,
    UploadOperationToken,
)
from personal_os.small_file_sync.errors import SmallFileSyncError
from personal_os.small_file_sync.metrics import (
    InMemorySmallFileSyncMetrics,
    SmallFileSyncMetrics,
)
from personal_os.small_file_sync.ports import (
    SmallFileBoundOperation,
)
from personal_os.small_file_sync.service import SmallFileSyncService
from personal_os.sources.commands import CreateSourceVersion, UpdateSourceVersion
from personal_os.sources.fingerprint import RequestFingerprint, SourceVersionCommand
from personal_os.sources.metrics import InMemorySourcePublicationMetrics, SourcePublicationMetrics
from personal_os.sources.ports import AwareUtcClock as SourceAwareUtcClock
from personal_os.sources.ports import PolicyEnforcementGuard, SourcePublicationStore
from personal_os.sources.publication import SourceVersionPublicationService
from personal_os.sources.reading import (
    CanonicalReadStateError,
    CanonicalSourceReference,
    ReadCurrentSourceCommand,
)
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult
from postgresql_source_store.canonical_read import PostgresqlCanonicalSourceReadStore
from postgresql_source_store.policy_enforcement import compose_policy_enforcement
from postgresql_source_store.publication_store import PostgresqlSourcePublicationStore
from postgresql_source_store.small_file_sync_operations import (
    PostgresqlSmallFileUploadOperationStore,
)
from r2_object_storage.adapter import R2S3ObjectStore
from r2_object_storage.client import (
    GetObjectResult,
    HeadObjectResult,
    PutObjectRequest,
    R2ClientManager,
)
from r2_object_storage.error_mapping import RetryPolicy
from r2_object_storage.metrics import InMemoryObjectStorageMetrics
from r2_object_storage.settings import LoadedR2Credentials, ObjectStorageSettings
from r2_object_storage.spool import SpoolManager

#: Server-owned operation lifetime of the offline rows, mirroring the durable
#: adapter's fifteen-minute reservation window.
_OFFLINE_EXPIRY_SECONDS: Final[int] = 900

_PENDING_STATE: Final[str] = "pending"
_COMMITTED_STATE: Final[str] = "committed"


@dataclass(frozen=True, slots=True)
class SmallFileSyncRuntime:
    """One composed small-file sync runtime the sync routes consume.

    ``aclose`` is the serve graph's disposal hook — closing the R2 client and
    its spool reservations on shutdown; the offline graph owns no resource
    and leaves it unset.
    """

    service: SmallFileSyncService
    aclose: Callable[[], Awaitable[None]] | None = None


class PolicyEnforcementSmallFileGuard:
    """Locator-aware small-file policy guard over the real enforcement service.

    Builds the capture-shaped subject the plugin gates locally (workspace,
    the update's canonical source identity when one exists, the normalized
    locator, the declared media type and size) and evaluates it through
    ``authorize_preflight`` at the single-part-upload boundary. A definite
    exclusion, an indeterminate outcome or any fail-closed policy failure
    propagates as the typed exclusion denial; an allowed decision becomes a
    server-owned revision binding without retaining the full decision evidence.
    """

    def __init__(self, *, enforcement: PolicyEnforcementService) -> None:
        self.enforcement = enforcement

    async def authorize_small_file(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> AllowedPolicyRevisionBinding:
        subject = PolicySubject(
            workspace_id=device_context.workspace_id,
            source_id=preflight.source_id,
            normalized_locator=preflight.normalized_locator.value,
            media_type=preflight.media_type,
            size_bytes=preflight.size_bytes,
        )
        decision = await self.enforcement.authorize_preflight(
            subject=subject,
            boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
            context=diagnostic_context,
        )
        return AllowedPolicyRevisionBinding(
            workspace_id=decision.workspace_id,
            policy_revision_number=decision.revision_number,
        )


@dataclass(frozen=True, slots=True)
class _BoundPolicyPublicationGuard:
    """One immutable authorization guard for one gateway invocation."""

    enforcement: PolicyEnforcementService
    binding: AllowedPolicyRevisionBinding

    async def authorize_publication(
        self,
        command: SourceVersionCommand,
        diagnostic_context: DiagnosticContext,
    ) -> PublicationPolicyEvidence:
        return await self.enforcement.authorize_bound_publication(
            command,
            self.binding,
            diagnostic_context,
        )


@dataclass(frozen=True, slots=True)
class BoundPolicySmallFilePublicationGateway:
    """Create a fresh immutable policy guard for every small-file publish."""

    store: SourcePublicationStore
    object_store: CanonicalObjectStore
    metrics: SourcePublicationMetrics
    clock: SourceAwareUtcClock
    enforcement: PolicyEnforcementService

    async def publish_create(
        self,
        *,
        command: CreateSourceVersion,
        stream: AsyncIterable[bytes],
        policy_binding: AllowedPolicyRevisionBinding,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        self._validate_workspace(command, policy_binding)
        publication_service = SourceVersionPublicationService(
            store=self.store,
            object_store=self.object_store,
            metrics=self.metrics,
            clock=self.clock,
            policy_guard=cast(
                "PolicyEnforcementGuard",
                _BoundPolicyPublicationGuard(
                    enforcement=self.enforcement,
                    binding=policy_binding,
                ),
            ),
        )
        return await publication_service.publish_create(
            command=command,
            stream=stream,
            diagnostic_context=diagnostic_context,
        )

    async def publish_update(
        self,
        *,
        command: UpdateSourceVersion,
        stream: AsyncIterable[bytes],
        policy_binding: AllowedPolicyRevisionBinding,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        self._validate_workspace(command, policy_binding)
        publication_service = SourceVersionPublicationService(
            store=self.store,
            object_store=self.object_store,
            metrics=self.metrics,
            clock=self.clock,
            policy_guard=cast(
                "PolicyEnforcementGuard",
                _BoundPolicyPublicationGuard(
                    enforcement=self.enforcement,
                    binding=policy_binding,
                ),
            ),
        )
        return await publication_service.publish_update(
            command=command,
            stream=stream,
            diagnostic_context=diagnostic_context,
        )

    @staticmethod
    def _validate_workspace(
        command: SourceVersionCommand,
        policy_binding: AllowedPolicyRevisionBinding,
    ) -> None:
        if command.workspace_id != policy_binding.workspace_id:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)


class LazyR2ClientSource:
    """Synchronous R2 client facade over the lazy per-process client manager.

    The serve composition runs before any event loop exists, while the R2
    client is created asynchronously; every member resolves the manager's
    cached client first — one bounded lock check after the first call — so
    the store never opens a connection at composition time and always talks
    to the one client owned by the serving loop.
    """

    def __init__(self, manager: R2ClientManager) -> None:
        self._manager = manager

    async def head_object(self, object_key: CanonicalObjectKey) -> HeadObjectResult | None:
        return await (await self._manager.get_client()).head_object(object_key)

    async def put_object(self, request: PutObjectRequest) -> None:
        await (await self._manager.get_client()).put_object(request)

    async def get_object(self, object_key: CanonicalObjectKey, *, if_match: str) -> GetObjectResult:
        return await (await self._manager.get_client()).get_object(object_key, if_match=if_match)

    async def head_bucket(self) -> None:
        await (await self._manager.get_client()).head_bucket()

    async def close(self) -> None:
        await self._manager.close()


def compose_small_file_sync(
    *,
    engine: AsyncEngine,
    signer: Ed25519PolicySigner,
    object_storage_settings: ObjectStorageSettings,
    object_storage_credentials: LoadedR2Credentials,
    logger: DiagnosticLogger,
) -> SmallFileSyncRuntime:
    """Build the real serve runtime of one API process.

    Follows the exclusion-policy serve precedent's shape: the shared engine,
    the signer-derived trust anchor verifier, and provider adapters that open
    no connection at construction — the PostgreSQL stores connect lazily per
    transaction and the R2 client opens at the first object-store call inside
    the serving loop. The graph is therefore composable before the socket
    exists while every adapter is the production one.
    """

    verifier = KeyedTrustAnchorVerifier(
        keyed_verifier=Ed25519PolicyVerifier({signer.key_id: signer.public_key_bytes})
    )
    enforcement = compose_policy_enforcement(engine, verifier=verifier)
    object_store = R2S3ObjectStore(
        LazyR2ClientSource(R2ClientManager(object_storage_settings, object_storage_credentials)),
        spools=SpoolManager(object_storage_settings.object_storage_spool_root),
        retry=RetryPolicy(),
        metrics=InMemoryObjectStorageMetrics(),
        logger=logger,
    )
    publication_gateway = BoundPolicySmallFilePublicationGateway(
        store=PostgresqlSourcePublicationStore(engine, policy_verifier=verifier),
        object_store=object_store,
        metrics=InMemorySourcePublicationMetrics(),
        clock=default_utc_clock,
        enforcement=enforcement,
    )
    service = SmallFileSyncService(
        operation_store=PostgresqlSmallFileUploadOperationStore(engine, clock=default_utc_clock),
        policy_guard=PolicyEnforcementSmallFileGuard(enforcement=enforcement),
        publication_gateway=publication_gateway,
        object_store=object_store,
        current_sources=PostgresqlCanonicalSourceReadStore(engine, policy_verifier=verifier),
        metrics=InMemorySmallFileSyncMetrics(),
        clock=default_utc_clock,
    )
    return SmallFileSyncRuntime(service=service, aclose=object_store.close)


class OfflineSmallFileClock:
    """Aware UTC clock reading the offline state's mutable frozen moment."""

    def __init__(self, state: OfflineSmallFileSyncState) -> None:
        self._state = state

    def __call__(self) -> datetime:
        if self._state.now is not None:
            return self._state.now
        return datetime.now(UTC)


@dataclass
class _OfflineOperationRow:
    """One durable operation row as the offline store keeps it."""

    operation_token: UploadOperationToken
    preflight: SmallFilePreflight
    device_context: SmallFileDeviceContext
    reserved_source_id: UUID | None
    expires_at: datetime
    state: str
    policy_revision_number: int
    terminal_result: SmallFileTerminalResult | None = None


@dataclass
class OfflineSmallFileSyncState:
    """Public knobs and safety counters of the offline small-file graph.

    Tests seed behavior through ``is_policy_denied`` (the locator-aware guard
    raises the typed exclusion denial while it is set), ``current_reference``
    (the update-base resolver returns it, or raises the typed read-state
    error while it is ``None``) and ``now`` (the frozen clock moment; expiry
    and terminal timestamps derive from it). The counters prove safety
    without retaining bytes: reservations, stored digests, publication
    commits and inserted source rows.
    """

    is_policy_denied: bool = False
    active_policy_revision_number: int = 1
    current_reference: CanonicalSourceReference | None = None
    now: datetime | None = None
    rows: list[_OfflineOperationRow] = field(default_factory=list)
    stored_digests: set[str] = field(default_factory=set)
    published_source_ids: set[UUID] = field(default_factory=set)
    publication_commits: int = 0

    @property
    def reservation_count(self) -> int:
        return len(self.rows)

    @property
    def stored_digest_count(self) -> int:
        return len(self.stored_digests)


def _identity(
    preflight: SmallFilePreflight, device_context: SmallFileDeviceContext
) -> tuple[UUID, UUID, UUID, str]:
    return (
        device_context.workspace_id,
        device_context.device_id,
        preflight.event_id,
        preflight.idempotency_key.value,
    )


def _fingerprint_matches(row: SmallFilePreflight, candidate: SmallFilePreflight) -> bool:
    return (
        row.operation is candidate.operation
        and row.sha256 == candidate.sha256
        and row.size_bytes == candidate.size_bytes
        and row.media_type == candidate.media_type
        and row.source_id == candidate.source_id
        and row.base_version_id == candidate.base_version_id
    )


class OfflineSmallFileUploadOperationStore:
    """In-memory operation store mirroring the durable adapter semantics."""

    def __init__(self, state: OfflineSmallFileSyncState, clock: OfflineSmallFileClock) -> None:
        self._state = state
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock()

    def _identity_row(
        self, preflight: SmallFilePreflight, device_context: SmallFileDeviceContext
    ) -> _OfflineOperationRow | None:
        identity = _identity(preflight, device_context)
        for row in self._state.rows:
            if _identity(row.preflight, row.device_context) == identity:
                return row
        return None

    def _token_row(self, operation_token: UploadOperationToken) -> _OfflineOperationRow | None:
        for row in self._state.rows:
            if row.operation_token.value == operation_token.value:
                return row
        return None

    async def resolve_terminal_result(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileTerminalResult | None:
        del diagnostic_context
        row = self._identity_row(preflight, device_context)
        if row is None:
            return None
        if not _fingerprint_matches(row.preflight, preflight):
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH)
        return row.terminal_result

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
        now = self._now()
        row = self._identity_row(preflight, device_context)
        if row is None:
            row = _OfflineOperationRow(
                operation_token=UploadOperationToken(secrets.token_urlsafe(32)),
                preflight=preflight,
                device_context=device_context,
                reserved_source_id=(
                    uuid4() if preflight.operation is SmallFileOperation.CREATE else None
                ),
                expires_at=now + timedelta(seconds=_OFFLINE_EXPIRY_SECONDS),
                state=_PENDING_STATE,
                policy_revision_number=policy_binding.policy_revision_number,
            )
            self._state.rows.append(row)
        else:
            if not _fingerprint_matches(row.preflight, preflight):
                raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH)
            if row.state == _COMMITTED_STATE:
                raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
            # Mirroring the durable adapter: an expired non-terminal row
            # re-reserves — fresh token, extended deadline — because nothing
            # was committed for it; receive-time expiry checks keep refusing
            # the continuation of any token past its deadline.
            if row.expires_at <= now:
                row.expires_at = now + timedelta(seconds=_OFFLINE_EXPIRY_SECONDS)
            row.operation_token = UploadOperationToken(secrets.token_urlsafe(32))
            row.policy_revision_number = policy_binding.policy_revision_number
        return SmallFileUploadOperation(
            operation_token=row.operation_token,
            preflight=preflight,
            device_context=device_context,
            reserved_source_id=row.reserved_source_id,
            expires_at=row.expires_at,
        )

    async def record_terminal_result(
        self,
        operation: SmallFileUploadOperation,
        result: SmallFileTerminalResult,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del diagnostic_context
        row = self._token_row(operation.operation_token)
        if row is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND)
        self._apply_terminal_transition(row, result)

    async def resolve_bound_operation(
        self,
        operation_token: UploadOperationToken,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileBoundOperation:
        del diagnostic_context
        row = self._token_row(operation_token)
        if row is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND)
        if (
            row.device_context.workspace_id != device_context.workspace_id
            or row.device_context.device_id != device_context.device_id
        ):
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH)
        if row.state != _COMMITTED_STATE and row.expires_at <= self._now():
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_EXPIRED)
        return SmallFileBoundOperation(
            operation_token=row.operation_token,
            workspace_id=row.device_context.workspace_id,
            device_id=row.device_context.device_id,
            event_id=row.preflight.event_id,
            idempotency_key=row.preflight.idempotency_key,
            operation=row.preflight.operation,
            declared_sha256=row.preflight.sha256,
            declared_size_bytes=row.preflight.size_bytes,
            declared_media_type=row.preflight.media_type,
            policy_revision_number=row.policy_revision_number,
            reserved_source_id=row.reserved_source_id,
            update_source_id=row.preflight.source_id,
            update_base_version_id=row.preflight.base_version_id,
            expires_at=row.expires_at,
            terminal_result=row.terminal_result,
        )

    async def record_bound_terminal_result(
        self,
        bound: SmallFileBoundOperation,
        result: SmallFileTerminalResult,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del diagnostic_context
        row = self._token_row(bound.operation_token)
        if row is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND)
        self._apply_terminal_transition(row, result)

    def _apply_terminal_transition(
        self, row: _OfflineOperationRow, result: SmallFileTerminalResult
    ) -> None:
        if row.state == _COMMITTED_STATE:
            if row.terminal_result == result:
                return
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        if row.expires_at <= self._now():
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_EXPIRED)
        row.state = _COMMITTED_STATE
        row.terminal_result = result


class OfflineSmallFilePolicyGuard:
    """Locator-aware guard double honoring the state's denial knob."""

    def __init__(self, state: OfflineSmallFileSyncState) -> None:
        self._state = state

    async def authorize_small_file(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> AllowedPolicyRevisionBinding:
        del preflight, diagnostic_context
        if self._state.is_policy_denied:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)
        return AllowedPolicyRevisionBinding(
            workspace_id=device_context.workspace_id,
            policy_revision_number=self._state.active_policy_revision_number,
        )


class OfflineCurrentSourceStore:
    """Current-source resolver double over the state's seeded reference."""

    def __init__(self, state: OfflineSmallFileSyncState) -> None:
        self._state = state

    async def resolve_current(
        self, command: ReadCurrentSourceCommand, diagnostic_context: DiagnosticContext
    ) -> CanonicalSourceReference:
        del diagnostic_context
        reference = self._state.current_reference
        if reference is None:
            raise CanonicalReadStateError(source_id=command.source_id)
        return reference


class _OfflineVerifiedObjectReader:
    """Bounded async reader over one stored offline content buffer."""

    def __init__(self, content: bytes) -> None:
        self._remaining = content

    async def read(self, size_bytes: int = 1_048_576) -> bytes:
        chunk = self._remaining[: max(size_bytes, 0)]
        self._remaining = self._remaining[len(chunk) :]
        return chunk

    def __aiter__(self) -> _OfflineVerifiedObjectReader:
        return self

    async def __anext__(self) -> bytes:
        if not self._remaining:
            raise StopAsyncIteration
        chunk = self._remaining[:65536]
        self._remaining = self._remaining[len(chunk) :]
        return chunk


class OfflineCanonicalObjectStore:
    """Object-store double verifying the real size and digest of the stream.

    Stored content stays in memory keyed by digest so the verification and
    reader members stay faithful to the provider-neutral port; the offline
    state exposes only the digest set as its public safety counter, never the
    bytes.
    """

    def __init__(self, state: OfflineSmallFileSyncState, clock: OfflineSmallFileClock) -> None:
        self._state = state
        self._clock = clock
        self._content_by_digest: dict[str, bytes] = {}

    def _receipt(self, expected: ExpectedObject) -> VerifiedObjectReceipt:
        return VerifiedObjectReceipt(
            content_digest=expected.content_digest,
            object_key=derive_canonical_object_key(expected.content_digest),
            size_bytes=expected.size_bytes,
            media_type=expected.media_type,
            verified_at=self._clock(),
            verification_method=VerificationMethod.UPLOADED_FULL_READ,
        )

    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None:
        if expected.content_digest.hexadecimal in self._state.stored_digests:
            return self._receipt(expected)
        return None

    async def store_stream(
        self,
        stream: AsyncIterable[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt:
        content = b"".join([chunk async for chunk in stream])
        if len(content) != expected_size_bytes:
            raise ObjectStorageError(
                ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
                safe_details={"reason": SIZE_MISMATCH},
            )
        computed = ContentDigest.parse(hashlib.sha256(content).hexdigest())
        if claimed_sha256 is not None and computed.hexadecimal != claimed_sha256:
            raise ObjectStorageError(
                ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
                safe_details={"reason": DIGEST_MISMATCH},
            )
        self._state.stored_digests.add(computed.hexadecimal)
        self._content_by_digest[computed.hexadecimal] = content
        return self._receipt(
            ExpectedObject(
                content_digest=computed,
                size_bytes=len(content),
                media_type=CanonicalMediaType.parse(media_type),
            )
        )

    async def verify_existing_object(self, expected: ExpectedObject) -> VerifiedObjectReceipt:
        if expected.content_digest.hexadecimal not in self._content_by_digest:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_OBJECT_MISSING)
        return self._receipt(expected)

    def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AbstractAsyncContextManager[_OfflineVerifiedObjectReader]:
        @asynccontextmanager
        async def _reader() -> AsyncIterator[_OfflineVerifiedObjectReader]:
            content = self._content_by_digest.get(expected.content_digest.hexadecimal)
            if content is None:
                raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_OBJECT_MISSING)
            yield _OfflineVerifiedObjectReader(content)

        return _reader()


@dataclass(frozen=True, slots=True)
class _OfflineBoundPolicyPublicationGuard:
    """Deterministically allow one explicitly supplied offline binding."""

    binding: AllowedPolicyRevisionBinding

    async def authorize_publication(
        self,
        command: SourceVersionCommand,
        diagnostic_context: DiagnosticContext,
    ) -> PublicationPolicyEvidence:
        del diagnostic_context
        if command.workspace_id != self.binding.workspace_id:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        return self.binding


@dataclass(frozen=True, slots=True)
class _OfflineSmallFilePublicationGateway:
    """Offline gateway preserving the invocation-local binding contract."""

    store: SourcePublicationStore
    object_store: CanonicalObjectStore
    metrics: SourcePublicationMetrics
    clock: SourceAwareUtcClock

    async def publish_create(
        self,
        *,
        command: CreateSourceVersion,
        stream: AsyncIterable[bytes],
        policy_binding: AllowedPolicyRevisionBinding,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        publication_service = self._publication_service(policy_binding)
        return await publication_service.publish_create(
            command=command,
            stream=stream,
            diagnostic_context=diagnostic_context,
        )

    async def publish_update(
        self,
        *,
        command: UpdateSourceVersion,
        stream: AsyncIterable[bytes],
        policy_binding: AllowedPolicyRevisionBinding,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        publication_service = self._publication_service(policy_binding)
        return await publication_service.publish_update(
            command=command,
            stream=stream,
            diagnostic_context=diagnostic_context,
        )

    def _publication_service(
        self, policy_binding: AllowedPolicyRevisionBinding
    ) -> SourceVersionPublicationService:
        return SourceVersionPublicationService(
            store=self.store,
            object_store=self.object_store,
            metrics=self.metrics,
            clock=self.clock,
            policy_guard=cast(
                "PolicyEnforcementGuard",
                _OfflineBoundPolicyPublicationGuard(binding=policy_binding),
            ),
        )


class OfflineSourcePublicationStore:
    """Idempotent in-memory publication store over the offline counters."""

    def __init__(self, state: OfflineSmallFileSyncState, clock: OfflineSmallFileClock) -> None:
        self._state = state
        self._clock = clock
        self._results: list[SourceVersionPublicationResult] = []

    def _committed(self, command: SourceVersionCommand) -> SourceVersionPublicationResult | None:
        for result in self._results:
            if result.source_id == command.source_id and result.event_id == command.event_id:
                return result
        return None

    async def resolve_committed(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult | None:
        del request_fingerprint, diagnostic_context
        return self._committed(command)

    async def commit_create(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        *,
        preflight_decision: PublicationPolicyEvidence | None = None,
    ) -> SourceVersionPublicationResult:
        del receipt, preflight_decision
        return self._commit(command, diagnostic_context, is_create=True)

    async def commit_update(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        *,
        preflight_decision: PublicationPolicyEvidence | None = None,
    ) -> SourceVersionPublicationResult:
        del receipt, preflight_decision
        return self._commit(command, diagnostic_context, is_create=False)

    def _commit(
        self,
        command: SourceVersionCommand,
        diagnostic_context: DiagnosticContext,
        *,
        is_create: bool,
    ) -> SourceVersionPublicationResult:
        del diagnostic_context
        committed = self._committed(command)
        if committed is not None:
            return committed
        result = SourceVersionPublicationResult(
            source_id=command.source_id,
            source_version_id=uuid4(),
            content_version=1 if is_create else 2,
            event_id=command.event_id,
            event_sequence=1,
            content_digest=command.expected_object.content_digest,
            outcome=PublicationOutcome.PUBLISHED,
            committed_at=self._clock(),
        )
        if is_create:
            self._state.published_source_ids.add(command.source_id)
        self._state.publication_commits += 1
        self._results.append(result)
        return result


def compose_offline_small_file_sync(
    *,
    state: OfflineSmallFileSyncState | None = None,
    metrics: SmallFileSyncMetrics | None = None,
) -> SmallFileSyncRuntime:
    """Build the deterministic offline small-file sync runtime."""

    offline_state = state if state is not None else OfflineSmallFileSyncState()
    clock = OfflineSmallFileClock(offline_state)
    object_store = OfflineCanonicalObjectStore(offline_state, clock)
    publication_store = OfflineSourcePublicationStore(offline_state, clock)
    publication_gateway = _OfflineSmallFilePublicationGateway(
        store=publication_store,
        object_store=object_store,
        metrics=InMemorySourcePublicationMetrics(),
        clock=clock,
    )
    service = SmallFileSyncService(
        operation_store=OfflineSmallFileUploadOperationStore(offline_state, clock),
        policy_guard=OfflineSmallFilePolicyGuard(offline_state),
        publication_gateway=publication_gateway,
        object_store=object_store,
        current_sources=OfflineCurrentSourceStore(offline_state),
        metrics=metrics if metrics is not None else InMemorySmallFileSyncMetrics(),
        clock=clock,
    )
    return SmallFileSyncRuntime(service=service)


__all__ = [
    "BoundPolicySmallFilePublicationGateway",
    "LazyR2ClientSource",
    "OfflineCanonicalObjectStore",
    "OfflineCurrentSourceStore",
    "OfflineSmallFileClock",
    "OfflineSmallFileSyncState",
    "OfflineSmallFileUploadOperationStore",
    "OfflineSourcePublicationStore",
    "PolicyEnforcementSmallFileGuard",
    "SmallFileSyncRuntime",
    "compose_offline_small_file_sync",
    "compose_small_file_sync",
]
