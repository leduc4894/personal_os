"""Framework-neutral orchestration of the small-file sync flows (spec 10).

:class:`SmallFileSyncService` owns the two-step upload of spec 10.1-10.3 over
injected ports only — the durable upload-operation store, the locator-aware
exclusion-policy guard, the current-source resolver, the canonical object
store, the existing
:class:`~personal_os.sources.publication.SourceVersionPublicationService`,
the closed low-cardinality metrics sink and the aware UTC clock. Preflight
re-evaluates policy server-side before any store or object-store access,
replays a frozen terminal result exactly, resolves the update base (stale,
missing, no-change or open) and otherwise reserves one short-lived opaque
operation whose reserved create UUID never inserts a ``sources`` row.
Receive binds to that exact operation by token, enforces the server-owned
single-part ceiling before any spool, streams through the server-side
bounded verification/CAS path, and hands the already-verified canonical
object to the publication service — never a raw byte and never an unverified
receipt — before freezing the publication transaction's result as the
operation's replayable terminal receipt.

The module imports no FastAPI, SQLAlchemy, R2 SDK or request type; it never
copies a locator, digest, token, byte count or provider detail into an error
message, safe detail or metric label. Two derivations are pinned here
because the durable operation row deliberately stores no path: the create's
canonical source type maps from the closed media-type vocabulary and its
title from one durable media-type label — a synthetic, stable, display-only
title child 5 (source locators) may refine.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.exclusion_policy.enforcement import AllowedPolicyRevisionBinding
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.object_storage import (
    CanonicalMediaType,
    CanonicalObjectStore,
    ExpectedObject,
)
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.small_file_sync.contracts import (
    MAX_SINGLE_PART_FILE_SIZE_BYTES,
    SmallFileDeviceContext,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFilePreflightOutcome,
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
    UploadOperationToken,
)
from personal_os.small_file_sync.errors import SmallFileSyncError
from personal_os.small_file_sync.metrics import (
    SmallFileMetricOutcome,
    SmallFileRejectionReason,
    SmallFileSyncMetrics,
)
from personal_os.small_file_sync.ports import (
    AwareUtcClock,
    SmallFileBoundOperation,
    SmallFilePolicyGuard,
    SmallFileUploadOperationStore,
)
from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    SourceTitle,
    SourceType,
    UpdateSourceVersion,
)
from personal_os.sources.publication import SourceVersionPublicationService
from personal_os.sources.reading import (
    CanonicalReadStateError,
    CanonicalSourceReadStore,
    ReadCurrentSourceCommand,
)
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult

#: Object-storage failure codes that are the closed content-integrity
#: rejection of spec 10.2/12: the received bytes failed the bounded size or
#: digest verification, so nothing may publish. Every other object-storage
#: failure (availability, authorization, configuration) keeps its own typed
#: error and never masquerades as an integrity verdict.
_RECEIVE_INTEGRITY_FAILURE_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
        ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED,
    }
)

#: Exact media types with a dedicated canonical source type; prefixed families
#: map below them and everything else fails over to the generic text type
#: (``web``/``youtube`` are ingestion-only types a device upload never mints).
_SOURCE_TYPE_BY_MEDIA_TYPE: Final[Mapping[str, SourceType]] = MappingProxyType(
    {
        "text/markdown": SourceType.MARKDOWN,
        "application/pdf": SourceType.PDF,
    }
)

#: The stable display-only create title for each mappable source type. The
#: durable operation row stores no path, so the canonical title is derived
#: from durable fields alone and is identical on every replay.
_CREATE_TITLE_BY_SOURCE_TYPE: Final[Mapping[SourceType, str]] = MappingProxyType(
    {
        SourceType.MARKDOWN: "Markdown file",
        SourceType.TEXT: "Text file",
        SourceType.PDF: "PDF document",
        SourceType.IMAGE: "Image file",
        SourceType.AUDIO: "Audio file",
    }
)


def derive_source_type(media_type: CanonicalMediaType) -> SourceType:
    """Map one canonical media type onto the closed create source-type set.

    ``text/markdown`` and ``application/pdf`` map exactly; the ``image/``,
    ``audio/`` and remaining ``text/`` families map by prefix; anything else
    (including ``application/octet-stream``) fails over to the generic text
    type so a create command always carries a closed member.
    """

    if media_type.value in _SOURCE_TYPE_BY_MEDIA_TYPE:
        return _SOURCE_TYPE_BY_MEDIA_TYPE[media_type.value]
    if media_type.value.startswith("image/"):
        return SourceType.IMAGE
    if media_type.value.startswith("audio/"):
        return SourceType.AUDIO
    return SourceType.TEXT


def derive_create_title(media_type: CanonicalMediaType) -> SourceTitle:
    """Derive the create's stable display-only title from durable fields."""

    source_type = derive_source_type(media_type)
    return SourceTitle(_CREATE_TITLE_BY_SOURCE_TYPE[source_type])


def terminal_result_from_publication(
    result: SourceVersionPublicationResult,
) -> SmallFileTerminalResult:
    """Freeze one publication transaction result as the replayable receipt.

    A published outcome is the ``committed`` receipt; the publication store's
    own no-change outcome is the ``no_change`` receipt over the unchanged
    current version. No digest, object key or receipt field crosses over.
    """

    result_kind = (
        SmallFileTerminalResultKind.COMMITTED
        if result.outcome is PublicationOutcome.PUBLISHED
        else SmallFileTerminalResultKind.NO_CHANGE
    )
    return SmallFileTerminalResult(
        result_kind=result_kind,
        source_id=result.source_id,
        source_version_id=result.source_version_id,
        content_version=result.content_version,
        committed_at=result.committed_at,
    )


class _ExhaustedByteStream:
    """Already-exhausted fallback stream for the receipt-resolved publication.

    The receive path spools and fully verifies the client stream itself, so
    the publication service's resolve-first content-addressable lookup hits
    and this stream is never consumed. Should the verified object vanish
    between the two steps, storing an exhausted stream fails the size
    verification closed instead of publishing anything unverified.
    """

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration


_EXHAUSTED_BYTE_STREAM: Final[AsyncIterable[bytes]] = _ExhaustedByteStream()


@dataclass(frozen=True, slots=True)
class SmallFilePreflightResult:
    """One completed preflight: exactly one typed outcome and its safe payload.

    ``single_part_upload`` carries only the opaque operation token and its
    expiry — never the reserved create UUID, a receipt or any object-store
    detail. ``committed_replay`` and ``no_change`` carry the frozen terminal
    receipt; ``excluded`` and ``conflict`` carry no payload at all.
    """

    outcome: SmallFilePreflightOutcome
    terminal_result: SmallFileTerminalResult | None = None
    operation_token: UploadOperationToken | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.outcome is SmallFilePreflightOutcome.SINGLE_PART_UPLOAD:
            if self.operation_token is None or self.expires_at is None:
                raise ValueError("single_part_upload requires the operation token and expiry")
            if self.terminal_result is not None:
                raise ValueError("single_part_upload carries no terminal result")
            return
        if self.outcome in (
            SmallFilePreflightOutcome.COMMITTED_REPLAY,
            SmallFilePreflightOutcome.NO_CHANGE,
        ):
            if self.terminal_result is None:
                raise ValueError("replay outcomes require the frozen terminal result")
            if self.operation_token is not None or self.expires_at is not None:
                raise ValueError("replay outcomes allocate no upload operation")
            return
        if (
            self.terminal_result is not None
            or self.operation_token is not None
            or self.expires_at is not None
        ):
            raise ValueError("excluded and conflict outcomes carry no safe payload")


@dataclass(slots=True)
class SmallFileSyncService:
    """Orchestrates preflight, receive and canonical publication (spec 10).

    Depends only on provider-neutral ports: the durable
    :class:`~personal_os.small_file_sync.ports.SmallFileUploadOperationStore`,
    the locator-aware
    :class:`~personal_os.small_file_sync.ports.SmallFilePolicyGuard`, the
    read-only current-source resolver for update-base checks, the canonical
    object store's bounded spool/verification path, the existing
    :class:`SourceVersionPublicationService` (the only path that turns
    verified bytes into canonical source versions), the closed metrics sink
    and the aware UTC clock. Expiry and state transitions of the durable
    operation row are the store's own authority: this service never
    re-derives them and never masks the store's closed errors.
    """

    operation_store: SmallFileUploadOperationStore
    policy_guard: SmallFilePolicyGuard
    publication_service: SourceVersionPublicationService
    object_store: CanonicalObjectStore
    current_sources: CanonicalSourceReadStore
    metrics: SmallFileSyncMetrics
    clock: AwareUtcClock

    async def preflight(
        self,
        *,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFilePreflightResult:
        """Run one preflight: replay, policy, base and reservation checks."""

        started_at = self.clock()
        try:
            return await self._preflight_once(
                preflight=preflight,
                device_context=device_context,
                diagnostic_context=diagnostic_context,
                started_at=started_at,
            )
        except SmallFileSyncError as error:
            self._record_rejection(preflight.operation, error)
            raise

    async def receive(
        self,
        *,
        operation_token: UploadOperationToken,
        device_context: SmallFileDeviceContext,
        stream: AsyncIterable[bytes],
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileTerminalResult:
        """Bind one content stream to its operation and publish once."""

        started_at = self.clock()
        # A failed binding (unknown token, credential mismatch, expiry, dead
        # state) raises the store's own closed error unmetered: the rejection
        # metric's operation label is only knowable after the binding holds.
        bound = await self.operation_store.resolve_bound_operation(
            operation_token, device_context, diagnostic_context
        )
        if bound.terminal_result is not None:
            # Response-loss replay: the operation already committed, so the
            # frozen receipt returns unchanged and nothing re-publishes.
            self.metrics.record_replay(operation=bound.operation)
            return bound.terminal_result
        try:
            return await self._receive_once(
                bound=bound,
                device_context=device_context,
                stream=stream,
                diagnostic_context=diagnostic_context,
                started_at=started_at,
            )
        except ApplicationError as error:
            upload_outcome = SmallFileMetricOutcome.REJECTED
            if isinstance(error, SmallFileSyncError):
                if error.error_code is ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED:
                    upload_outcome = SmallFileMetricOutcome.INTEGRITY_FAILED
                self._record_rejection(bound.operation, error)
            self.metrics.record_upload(
                operation=bound.operation,
                outcome=upload_outcome,
                duration_seconds=self._elapsed_seconds_since(started_at),
            )
            raise

    async def _preflight_once(
        self,
        *,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
        started_at: datetime,
    ) -> SmallFilePreflightResult:
        # Policy preflight (spec 9/10.1): the active signed policy is
        # re-evaluated server-side with the locator-aware subject before any
        # replay lookup, reservation or object-store access, so a denied or
        # indeterminate subject never receives canonical data or an upload.
        try:
            policy_binding = await self.policy_guard.authorize_small_file(
                preflight, device_context, diagnostic_context
            )
        except ExclusionPolicyError:
            self._record_preflight(
                preflight.operation, SmallFilePreflightOutcome.EXCLUDED, started_at
            )
            return SmallFilePreflightResult(outcome=SmallFilePreflightOutcome.EXCLUDED)
        # Exact replay (spec 10.3): a frozen terminal result returns
        # unchanged — committed replays as committed_replay, a frozen
        # no-change as no_change — without allocating another upload.
        frozen = await self.operation_store.resolve_terminal_result(
            preflight, device_context, diagnostic_context
        )
        if frozen is not None:
            outcome = (
                SmallFilePreflightOutcome.COMMITTED_REPLAY
                if frozen.result_kind is SmallFileTerminalResultKind.COMMITTED
                else SmallFilePreflightOutcome.NO_CHANGE
            )
            self.metrics.record_replay(operation=preflight.operation)
            self._record_preflight(preflight.operation, outcome, started_at)
            return SmallFilePreflightResult(outcome=outcome, terminal_result=frozen)
        if preflight.operation is SmallFileOperation.UPDATE:
            base_result = await self._check_update_base(
                preflight=preflight,
                device_context=device_context,
                policy_binding=policy_binding,
                diagnostic_context=diagnostic_context,
                started_at=started_at,
            )
            if base_result is not None:
                return base_result
        operation = await self.operation_store.reserve_operation(
            preflight=preflight,
            device_context=device_context,
            policy_binding=policy_binding,
            diagnostic_context=diagnostic_context,
        )
        self._record_preflight(
            preflight.operation, SmallFilePreflightOutcome.SINGLE_PART_UPLOAD, started_at
        )
        return SmallFilePreflightResult(
            outcome=SmallFilePreflightOutcome.SINGLE_PART_UPLOAD,
            operation_token=operation.operation_token,
            expires_at=operation.expires_at,
        )

    async def _check_update_base(
        self,
        *,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        policy_binding: AllowedPolicyRevisionBinding,
        diagnostic_context: DiagnosticContext,
        started_at: datetime,
    ) -> SmallFilePreflightResult | None:
        """Resolve the update base; ``None`` opens the single-part upload.

        A missing current reference or a base that is no longer current is
        the durable ``conflict`` outcome — no overwrite, no reservation. A
        current base whose committed digest equals the declared digest is the
        safe ``no_change`` receipt: the operation is reserved and the
        confirmed current base frozen as its terminal result so a lost
        response replays the exact no-op.
        """

        update_source_id = preflight.source_id
        update_base_version_id = preflight.base_version_id
        if update_source_id is None or update_base_version_id is None:
            # Unreachable for a validated update preflight; the closed shape
            # error keeps the path total.
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_PREFLIGHT_INVALID)
        try:
            reference = await self.current_sources.resolve_current(
                ReadCurrentSourceCommand(
                    workspace_id=device_context.workspace_id, source_id=update_source_id
                ),
                diagnostic_context,
            )
        except CanonicalReadStateError:
            self._record_preflight(
                preflight.operation, SmallFilePreflightOutcome.CONFLICT, started_at
            )
            return SmallFilePreflightResult(outcome=SmallFilePreflightOutcome.CONFLICT)
        if reference.source_version_id != update_base_version_id:
            self._record_preflight(
                preflight.operation, SmallFilePreflightOutcome.CONFLICT, started_at
            )
            return SmallFilePreflightResult(outcome=SmallFilePreflightOutcome.CONFLICT)
        if reference.expected_object.content_digest != preflight.sha256:
            return None
        operation = await self.operation_store.reserve_operation(
            preflight=preflight,
            device_context=device_context,
            policy_binding=policy_binding,
            diagnostic_context=diagnostic_context,
        )
        terminal = SmallFileTerminalResult(
            result_kind=SmallFileTerminalResultKind.NO_CHANGE,
            source_id=reference.source_id,
            source_version_id=reference.source_version_id,
            content_version=reference.content_version,
            committed_at=reference.committed_at,
        )
        await self.operation_store.record_terminal_result(
            operation, terminal, diagnostic_context
        )
        self._record_preflight(
            preflight.operation, SmallFilePreflightOutcome.NO_CHANGE, started_at
        )
        return SmallFilePreflightResult(
            outcome=SmallFilePreflightOutcome.NO_CHANGE, terminal_result=terminal
        )

    async def _receive_once(
        self,
        *,
        bound: SmallFileBoundOperation,
        device_context: SmallFileDeviceContext,
        stream: AsyncIterable[bytes],
        diagnostic_context: DiagnosticContext,
        started_at: datetime,
    ) -> SmallFileTerminalResult:
        # The server-owned single-part ceiling is enforced before any spool
        # or object-store access (spec 3.1/10.1); equality is allowed.
        if bound.declared_size_bytes > MAX_SINGLE_PART_FILE_SIZE_BYTES:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_SIZE_LIMIT_EXCEEDED)
        # A create must carry the canonical UUID the server reserved at
        # preflight; that reservation never inserted a ``sources`` row.
        if bound.operation is SmallFileOperation.CREATE and bound.reserved_source_id is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        # Spool through the server-side bounded verification/CAS path: full
        # byte-size and SHA-256 verification precede any publication, and a
        # mismatch is the closed integrity failure that never publishes.
        try:
            receipt = await self.object_store.store_stream(
                stream,
                bound.declared_size_bytes,
                bound.declared_media_type.value,
                bound.declared_sha256.hexadecimal,
            )
        except ObjectStorageError as error:
            if error.error_code in _RECEIVE_INTEGRITY_FAILURE_CODES:
                raise SmallFileSyncError(
                    ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED
                ) from error
            raise
        if (
            receipt.content_digest != bound.declared_sha256
            or receipt.size_bytes != bound.declared_size_bytes
            or receipt.media_type != bound.declared_media_type
        ):
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED)
        result = await self._publish(
            bound=bound,
            device_context=device_context,
            diagnostic_context=diagnostic_context,
        )
        terminal = terminal_result_from_publication(result)
        await self.operation_store.record_bound_terminal_result(
            bound, terminal, diagnostic_context
        )
        self.metrics.record_upload(
            operation=bound.operation,
            outcome=SmallFileMetricOutcome.COMMITTED,
            duration_seconds=self._elapsed_seconds_since(started_at),
        )
        return terminal

    async def _publish(
        self,
        *,
        bound: SmallFileBoundOperation,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        """Publish through the canonical service over the verified object.

        The publication command binds the operation's frozen identity: the
        credential-derived workspace and device actor, the journal event and
        idempotency key, the declared fingerprint as the expected object, and
        — for a create — the reserved canonical UUID with the derived
        durable-only type and title. The exhausted fallback stream is never
        consumed because the verified object resolves content-addressably.
        """

        expected_object = ExpectedObject(
            content_digest=bound.declared_sha256,
            size_bytes=bound.declared_size_bytes,
            media_type=bound.declared_media_type,
        )
        actor = SourceActor(
            actor_kind=ActorKind.DEVICE, actor_id=device_context.device_id
        )
        idempotency_key = IdempotencyKey(bound.idempotency_key.value)
        if bound.operation is SmallFileOperation.CREATE:
            reserved_source_id = bound.reserved_source_id
            if reserved_source_id is None:
                raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
            create_command = CreateSourceVersion(
                workspace_id=device_context.workspace_id,
                source_id=reserved_source_id,
                event_id=bound.event_id,
                idempotency_key=idempotency_key,
                source_type=derive_source_type(bound.declared_media_type),
                title=derive_create_title(bound.declared_media_type),
                actor=actor,
                expected_object=expected_object,
                client_timestamp=None,
            )
            return await self.publication_service.publish_create(
                command=create_command,
                stream=_EXHAUSTED_BYTE_STREAM,
                diagnostic_context=diagnostic_context,
            )
        update_source_id = bound.update_source_id
        update_base_version_id = bound.update_base_version_id
        if update_source_id is None or update_base_version_id is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        update_command = UpdateSourceVersion(
            workspace_id=device_context.workspace_id,
            source_id=update_source_id,
            event_id=bound.event_id,
            idempotency_key=idempotency_key,
            base_version_id=update_base_version_id,
            actor=actor,
            expected_object=expected_object,
            client_timestamp=None,
        )
        return await self.publication_service.publish_update(
            command=update_command,
            stream=_EXHAUSTED_BYTE_STREAM,
            diagnostic_context=diagnostic_context,
        )

    def _record_preflight(
        self,
        operation: SmallFileOperation,
        outcome: SmallFilePreflightOutcome,
        started_at: datetime,
    ) -> None:
        self.metrics.record_preflight(
            operation=operation,
            outcome=outcome,
            duration_seconds=self._elapsed_seconds_since(started_at),
        )

    def _record_rejection(
        self, operation: SmallFileOperation, error: SmallFileSyncError
    ) -> None:
        self.metrics.record_rejection(
            operation=operation,
            reason_code=SmallFileRejectionReason(error.error_code.value),
        )

    def _elapsed_seconds_since(self, started_at: datetime) -> float:
        # Clamped at zero so a clock seam that repeats or drifts backwards can
        # never turn a recorded duration negative.
        return max((self.clock() - started_at).total_seconds(), 0.0)


__all__ = [
    "SmallFilePreflightResult",
    "SmallFileSyncService",
    "derive_create_title",
    "derive_source_type",
    "terminal_result_from_publication",
]
