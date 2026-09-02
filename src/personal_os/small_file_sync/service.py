"""Framework-neutral orchestration of the small-file sync flows (spec 10).

:class:`SmallFileSyncService` owns the two-step upload of spec 10.1-10.3 over
injected ports only — the durable upload-operation store, the locator-aware
exclusion-policy guard, the current-source resolver, the canonical object
store, the existing
:class:`~personal_os.small_file_sync.ports.SmallFilePublicationGateway`,
the Child 8
:class:`~personal_os.small_file_sync.ports.SmallFileConflictCaptureGateway`,
the closed low-cardinality metrics sink and the aware UTC clock. Preflight
re-evaluates policy server-side before any store or object-store access,
replays a frozen terminal result exactly, resolves the update base (stale,
missing, no-change or open), routes a declared size strictly above the
unchanged single-part constant to the payload-free multipart outcome, and
otherwise reserves one short-lived opaque
operation whose reserved create UUID never inserts a ``sources`` row.
Receive binds to that exact operation by token, enforces the server-owned
single-part ceiling before any spool, streams through the server-side
bounded verification/CAS path, and hands the already-verified canonical
object to the publication service — never a raw byte and never an unverified
receipt — before freezing the publication transaction's result as the
operation's replayable terminal receipt. A typed non-retryable rejection
raised after the claim is persisted as the operation's terminal ``failed``
state carrying its closed registry token, then re-raised unchanged, so a
typed business rejection never leaves the claimed row fenced in
``receiving``; retryable and untyped failures keep their resume behavior.

The Child 8 conflict bridge (spec 5.1) rides the same two-step shape: a
single-part-sized update preflighted on a stale base — or on a source the
server deleted under the local edit — keeps its ``conflict`` verdict and
reserves one capture operation instead, a same-identity re-preflight
answers the stored conflict before the normal classifier, and
:meth:`SmallFileSyncService.receive_conflict_candidate` verifies the
candidate bytes through the identical bounded path before the
conflict-capture gateway retains them in place of publication — as a
``stale_content`` conflict when the base merely went stale and as an
``edit_remote_delete`` conflict when the current reference cannot be
served — capturing nothing on the current pointer and answering only the
opaque conflict identity, which both the same-token and same-event replays
return unchanged. A captured operation deliberately holds its claimed-row
fence: the durable store port exposes no honest terminal transition for a
capture, so the replay contract is carried by the conflict aggregate's own
event identity, and a same-token re-upload re-verifies and replays
idempotently.

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
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.exclusion_policy.enforcement import AllowedPolicyRevisionBinding
from personal_os.exclusion_policy.errors import ExclusionPolicyError, is_policy_system_failure
from personal_os.object_storage import (
    CanonicalMediaType,
    CanonicalObjectStore,
    ExpectedObject,
)
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.small_file_sync.contracts import (
    MAX_SINGLE_PART_FILE_SIZE_BYTES,
    SmallFileConflictCaptureResult,
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
    SmallFileConflictCaptureGateway,
    SmallFilePolicyGuard,
    SmallFilePublicationGateway,
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

#: The closed policy-failure codes recordable into the rejection diagnostics
#: ring (policy-observability remediation C1): the policy DENIAL codes keep
#: the terminal ``excluded`` preflight outcome while the SYSTEM codes
#: propagate as the typed errors behind the closed 409/503 envelopes — both
#: sides record their registry code so the operator surface carries the why.
_POLICY_REJECTION_REASON_BY_CODE: Final[Mapping[ErrorCode, SmallFileRejectionReason]] = (
    MappingProxyType(
        {
            ErrorCode.EXCLUSION_POLICY_DENIED: SmallFileRejectionReason.EXCLUSION_POLICY_DENIED,
            ErrorCode.EXCLUSION_POLICY_INDETERMINATE: (
                SmallFileRejectionReason.EXCLUSION_POLICY_INDETERMINATE
            ),
            ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED: (
                SmallFileRejectionReason.EXCLUSION_POLICY_NOT_INITIALIZED
            ),
            ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE: (
                SmallFileRejectionReason.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
            ),
        }
    )
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
    receipt; ``multipart_upload`` routes a file strictly above the single-part
    routing constant into the resumable multipart transport and — like
    ``excluded`` — carries no payload at all: the client obtains its opaque
    session, geometry and URLs only from the multipart session endpoints,
    never from this result. The Child 8 ``conflict`` outcome carries either
    the same opaque operation grant — a stale single-part-sized update whose
    verified candidate the client uploads for capture — or exactly the
    opaque conflict identity a same-identity replay returns after capture;
    a conflict that cannot retain bytes yet (a missing source, or a size
    above the single-part routing constant) carries no payload at all, and
    no conflict result ever carries a terminal result, receipt or raw
    content.
    """

    outcome: SmallFilePreflightOutcome
    terminal_result: SmallFileTerminalResult | None = None
    operation_token: UploadOperationToken | None = None
    expires_at: datetime | None = None
    conflict_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.outcome is SmallFilePreflightOutcome.SINGLE_PART_UPLOAD:
            if self.operation_token is None or self.expires_at is None:
                raise ValueError("single_part_upload requires the operation token and expiry")
            if self.terminal_result is not None:
                raise ValueError("single_part_upload carries no terminal result")
            if self.conflict_id is not None:
                raise ValueError("single_part_upload carries no conflict identity")
            return
        if self.outcome in (
            SmallFilePreflightOutcome.COMMITTED_REPLAY,
            SmallFilePreflightOutcome.NO_CHANGE,
        ):
            if self.terminal_result is None:
                raise ValueError("replay outcomes require the frozen terminal result")
            if self.operation_token is not None or self.expires_at is not None:
                raise ValueError("replay outcomes allocate no upload operation")
            if self.conflict_id is not None:
                raise ValueError("replay outcomes carry no conflict identity")
            return
        if self.outcome is SmallFilePreflightOutcome.CONFLICT:
            if self.terminal_result is not None:
                raise ValueError("a conflict outcome carries no terminal result")
            has_grant = self.operation_token is not None or self.expires_at is not None
            if has_grant and (self.operation_token is None or self.expires_at is None):
                raise ValueError("a conflict capture grant requires the token and expiry")
            if has_grant and self.conflict_id is not None:
                raise ValueError("a conflict capture grant carries no conflict identity")
            return
        if (
            self.terminal_result is not None
            or self.operation_token is not None
            or self.expires_at is not None
            or self.conflict_id is not None
        ):
            raise ValueError("excluded and multipart outcomes carry no safe payload")


@dataclass(slots=True)
class SmallFileSyncService:
    """Orchestrates preflight, receive and canonical publication (spec 10).

    Depends only on provider-neutral ports: the durable
    :class:`~personal_os.small_file_sync.ports.SmallFileUploadOperationStore`,
    the locator-aware
    :class:`~personal_os.small_file_sync.ports.SmallFilePolicyGuard`, the
    read-only current-source resolver for update-base checks, the canonical
    object store's bounded spool/verification path, the existing
    publication gateway (which invokes the only path that turns verified bytes
    into canonical source versions), the closed metrics sink
    and the aware UTC clock. Expiry and state transitions of the durable
    operation row are the store's own authority: this service never
    re-derives them and never masks the store's closed errors.
    """

    operation_store: SmallFileUploadOperationStore
    policy_guard: SmallFilePolicyGuard
    publication_gateway: SmallFilePublicationGateway
    object_store: CanonicalObjectStore
    current_sources: CanonicalSourceReadStore
    conflict_capture: SmallFileConflictCaptureGateway
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
            if not error.is_retryable:
                await self._persist_typed_rejection(bound, error.error_code, diagnostic_context)
            self.metrics.record_upload(
                operation=bound.operation,
                outcome=upload_outcome,
                duration_seconds=self._elapsed_seconds_since(started_at),
            )
            raise

    async def receive_conflict_candidate(
        self,
        *,
        operation_token: UploadOperationToken,
        device_context: SmallFileDeviceContext,
        stream: AsyncIterable[bytes],
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileConflictCaptureResult:
        """Bind one stale-update candidate stream to its capture operation.

        The Child 8 counterpart of :meth:`receive` over the same durable
        operation row and the same bounded verification path: the stream is
        spooled and fully verified before anything may reference it, the
        current source is re-read to freeze the remote the capture observed,
        and the verified candidate is handed to the conflict-capture gateway
        in place of publication. The returned receipt carries only the
        opaque conflict identity — never a publication receipt or raw
        content — and an exact replay of the same token or event identity
        returns the original conflict unchanged.
        """

        started_at = self.clock()
        # A failed binding (unknown token, credential mismatch, expiry, dead
        # state) raises the store's own closed error unmetered, exactly like
        # the publication receive.
        bound = await self.operation_store.resolve_bound_operation(
            operation_token, device_context, diagnostic_context
        )
        if bound.terminal_result is not None:
            # A publication operation never doubles as a capture operation.
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        try:
            return await self._capture_once(
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
            if not error.is_retryable:
                await self._persist_typed_rejection(bound, error.error_code, diagnostic_context)
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
        # A policy DENIAL keeps the terminal ``excluded`` outcome; a policy
        # SYSTEM failure (no active signed policy, corrupt signing material)
        # propagates as the typed error so the API answers with its closed
        # 409/503 envelope instead of collapsing a broken policy system into
        # a success shape. Both record the closed registry code into the
        # rejection ring (policy-observability remediation C1).
        try:
            policy_binding = await self.policy_guard.authorize_small_file(
                preflight, device_context, diagnostic_context
            )
        except ExclusionPolicyError as error:
            self._record_policy_rejection(preflight.operation, error.error_code)
            if is_policy_system_failure(error):
                raise
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
            # Conflict-membership replay (Child 8 spec 5.1): a captured event
            # answers with its stored conflict before the normal base
            # classifier can reserve or publish anything. The frozen
            # publication/no-change replay lookup above runs first because it
            # also carries the payload-substitution guard — a different
            # fingerprint under the same identity rejects there, so the
            # membership lookup can key on the event identity alone. A
            # captured event never holds a terminal publication result, so
            # the two replay families never collide.
            captured = await self.conflict_capture.resolve_captured_conflict(
                workspace_id=device_context.workspace_id,
                originating_event_id=preflight.event_id,
                diagnostic_context=diagnostic_context,
            )
            if captured is not None:
                self.metrics.record_replay(operation=preflight.operation)
                self._record_preflight(
                    preflight.operation, SmallFilePreflightOutcome.CONFLICT, started_at
                )
                return SmallFilePreflightResult(
                    outcome=SmallFilePreflightOutcome.CONFLICT,
                    conflict_id=captured.conflict_id,
                )
            base_result = await self._check_update_base(
                preflight=preflight,
                device_context=device_context,
                policy_binding=policy_binding,
                diagnostic_context=diagnostic_context,
                started_at=started_at,
            )
            if base_result is not None:
                return base_result
        # Multipart routing (Child 7 spec 4): a declared size strictly above
        # the unchanged single-part routing constant — and the preflight value
        # already capped it at the product maximum — never reserves a
        # single-part operation. The outcome is payload-free: the client
        # derives its opaque session, geometry and part URLs only from the
        # multipart session endpoints after this decision.
        if preflight.size_bytes > MAX_SINGLE_PART_FILE_SIZE_BYTES:
            self._record_preflight(
                preflight.operation, SmallFilePreflightOutcome.MULTIPART_UPLOAD, started_at
            )
            return SmallFilePreflightResult(outcome=SmallFilePreflightOutcome.MULTIPART_UPLOAD)
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

        The policy/base check itself is unchanged. A missing current
        reference — a source the server deleted under the local edit — keeps
        the durable ``conflict`` verdict and, for a single-part-sized update,
        reserves one capture operation whose grant the result carries: the
        client uploads its candidate through the conflict-candidate receive
        path, which verifies the bytes and captures them as
        ``edit_remote_delete`` evidence instead of publishing. A base that is
        no longer current keeps that same ``conflict`` verdict and grant
        shape, capturing instead as ``stale_content`` (Child 8 spec 5.1). A
        current base
        whose committed digest equals the declared digest is the safe
        ``no_change`` receipt: the operation is reserved and the confirmed
        current base frozen as its terminal result so a lost response
        replays the exact no-op. A typed policy DENIAL raised by the read
        boundary's locked recheck is the same terminal ``excluded`` outcome
        the authorize boundary produces (spec 9/10.1) — it never escapes as
        an error envelope the route would answer with 403. A policy SYSTEM
        failure propagates as the typed error behind the closed 409/503
        envelope instead (policy-observability remediation C1); both sides
        record their closed registry code into the rejection ring.
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
            # The current reference cannot be served — most notably a source
            # the server deleted under the local edit (an edit-remote-delete
            # race of Child 8 spec 4.1). The verdict stays ``conflict`` with
            # no overwrite and no reservation: a single-part-sized update
            # keeps one capture operation grant so the verified candidate is
            # retained as ``edit_remote_delete`` evidence, while larger sizes
            # keep the payload-free outcome exactly as before.
            if preflight.size_bytes > MAX_SINGLE_PART_FILE_SIZE_BYTES:
                self._record_preflight(
                    preflight.operation, SmallFilePreflightOutcome.CONFLICT, started_at
                )
                return SmallFilePreflightResult(outcome=SmallFilePreflightOutcome.CONFLICT)
            operation = await self.operation_store.reserve_operation(
                preflight=preflight,
                device_context=device_context,
                policy_binding=policy_binding,
                diagnostic_context=diagnostic_context,
            )
            self._record_preflight(
                preflight.operation, SmallFilePreflightOutcome.CONFLICT, started_at
            )
            return SmallFilePreflightResult(
                outcome=SmallFilePreflightOutcome.CONFLICT,
                operation_token=operation.operation_token,
                expires_at=operation.expires_at,
            )
        except ExclusionPolicyError as error:
            # The read boundary's transaction-final recheck denied or could
            # not decide the subject: the preflight outcome contract stays
            # total over policy DENIALS with the typed ``excluded`` outcome,
            # while a policy SYSTEM failure propagates as the typed error
            # (policy-observability remediation C1). Both record the closed
            # registry code into the rejection ring.
            self._record_policy_rejection(preflight.operation, error.error_code)
            if is_policy_system_failure(error):
                raise
            self._record_preflight(
                preflight.operation, SmallFilePreflightOutcome.EXCLUDED, started_at
            )
            return SmallFilePreflightResult(outcome=SmallFilePreflightOutcome.EXCLUDED)
        if reference.source_version_id != update_base_version_id:
            # Stale base (Child 8 spec 5.1): the verdict stays ``conflict``,
            # but a single-part-sized update now reserves one capture
            # operation so the client uploads its candidate through the same
            # verified receive path; capture — not publication — then retains
            # the bytes as conflict evidence. Above the single-part routing
            # constant no verified-object transport exists for the candidate
            # in this slice, so the outcome stays payload-free exactly as
            # before.
            if preflight.size_bytes > MAX_SINGLE_PART_FILE_SIZE_BYTES:
                self._record_preflight(
                    preflight.operation, SmallFilePreflightOutcome.CONFLICT, started_at
                )
                return SmallFilePreflightResult(outcome=SmallFilePreflightOutcome.CONFLICT)
            operation = await self.operation_store.reserve_operation(
                preflight=preflight,
                device_context=device_context,
                policy_binding=policy_binding,
                diagnostic_context=diagnostic_context,
            )
            self._record_preflight(
                preflight.operation, SmallFilePreflightOutcome.CONFLICT, started_at
            )
            return SmallFilePreflightResult(
                outcome=SmallFilePreflightOutcome.CONFLICT,
                operation_token=operation.operation_token,
                expires_at=operation.expires_at,
            )
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
        await self.operation_store.record_terminal_result(operation, terminal, diagnostic_context)
        self._record_preflight(preflight.operation, SmallFilePreflightOutcome.NO_CHANGE, started_at)
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
                raise SmallFileSyncError(ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED) from error
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
        await self.operation_store.record_bound_terminal_result(bound, terminal, diagnostic_context)
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
        """Publish through the bound gateway over the verified object.

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
        actor = SourceActor(actor_kind=ActorKind.DEVICE, actor_id=device_context.device_id)
        idempotency_key = IdempotencyKey(bound.idempotency_key.value)
        policy_binding = AllowedPolicyRevisionBinding(
            workspace_id=bound.workspace_id,
            policy_revision_number=bound.policy_revision_number,
        )
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
                initial_locator=bound.normalized_locator,
            )
            if (
                create_command.workspace_id != device_context.workspace_id
                or policy_binding.workspace_id != device_context.workspace_id
            ):
                raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
            return await self.publication_gateway.publish_create(
                command=create_command,
                stream=_EXHAUSTED_BYTE_STREAM,
                policy_binding=policy_binding,
                bound_operation=bound,
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
        if (
            update_command.workspace_id != device_context.workspace_id
            or policy_binding.workspace_id != device_context.workspace_id
        ):
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        return await self.publication_gateway.publish_update(
            command=update_command,
            stream=_EXHAUSTED_BYTE_STREAM,
            policy_binding=policy_binding,
            bound_operation=bound,
            diagnostic_context=diagnostic_context,
        )

    async def _capture_once(
        self,
        *,
        bound: SmallFileBoundOperation,
        device_context: SmallFileDeviceContext,
        stream: AsyncIterable[bytes],
        diagnostic_context: DiagnosticContext,
        started_at: datetime,
    ) -> SmallFileConflictCaptureResult:
        # Verified-object admission precedes everything (Child 8 spec 3.3):
        # the server-owned single-part ceiling and the full byte-size and
        # SHA-256 verification run before any conflict may reference the
        # bytes, and a mismatch is the closed integrity failure that never
        # captures.
        if bound.declared_size_bytes > MAX_SINGLE_PART_FILE_SIZE_BYTES:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_SIZE_LIMIT_EXCEEDED)
        if bound.operation is not SmallFileOperation.UPDATE:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        update_source_id = bound.update_source_id
        update_base_version_id = bound.update_base_version_id
        if update_source_id is None or update_base_version_id is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        try:
            receipt = await self.object_store.store_stream(
                stream,
                bound.declared_size_bytes,
                bound.declared_media_type.value,
                bound.declared_sha256.hexadecimal,
            )
        except ObjectStorageError as error:
            if error.error_code in _RECEIVE_INTEGRITY_FAILURE_CODES:
                raise SmallFileSyncError(ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED) from error
            raise
        if (
            receipt.content_digest != bound.declared_sha256
            or receipt.size_bytes != bound.declared_size_bytes
            or receipt.media_type != bound.declared_media_type
        ):
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED)
        # The observed remote is read at capture time — after verification —
        # so the frozen evidence describes exactly the world the capture
        # saw. A base that became current again is not a capturable stale
        # update: the closed state-invalid rejection releases the claim for
        # a same-identity re-preflight instead of capturing or publishing.
        try:
            reference = await self.current_sources.resolve_current(
                ReadCurrentSourceCommand(
                    workspace_id=device_context.workspace_id, source_id=update_source_id
                ),
                diagnostic_context,
            )
        except CanonicalReadStateError:
            # The current reference cannot be served. The gateway re-validates
            # the deletion against capture-time canonical state and retains the
            # verified candidate as an ``edit_remote_delete`` conflict; a race
            # it cannot confirm answers ``None`` and this receive fails closed
            # with no capture, no publication and the claim released for a
            # same-identity re-preflight (Child 8 spec 4.1 row 2).
            captured = await self.conflict_capture.capture_edit_remote_delete(
                bound_operation=bound,
                verified_candidate=receipt,
                diagnostic_context=diagnostic_context,
            )
            if captured is None:
                raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID) from None
            self.metrics.record_upload(
                operation=bound.operation,
                outcome=SmallFileMetricOutcome.CONFLICT_CAPTURED,
                duration_seconds=self._elapsed_seconds_since(started_at),
            )
            return captured
        if reference.source_version_id == update_base_version_id:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        captured = await self.conflict_capture.capture_stale_update(
            bound_operation=bound,
            verified_candidate=receipt,
            observed_remote_version_id=reference.source_version_id,
            diagnostic_context=diagnostic_context,
        )
        self.metrics.record_upload(
            operation=bound.operation,
            outcome=SmallFileMetricOutcome.CONFLICT_CAPTURED,
            duration_seconds=self._elapsed_seconds_since(started_at),
        )
        return captured

    async def _persist_typed_rejection(
        self,
        bound: SmallFileBoundOperation,
        error_code: ErrorCode,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        """Persist one typed non-retryable rejection as the claim's terminal state.

        The store's guarded ``receiving -> failed`` transition carries only
        the closed registry token. When the guarded write itself refuses —
        a concurrent terminal winner or a binding that drifted from its row —
        the original typed rejection still surfaces unchanged; the refused
        write names its own closed token on the rejection ring instead of
        being swallowed silently.
        """

        try:
            await self.operation_store.record_bound_terminal_failure(
                bound, error_code, diagnostic_context
            )
        except ApplicationError as write_error:
            self._record_rejection_code(bound.operation, write_error.error_code)

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

    def _record_rejection(self, operation: SmallFileOperation, error: SmallFileSyncError) -> None:
        self._record_rejection_code(operation, error.error_code)

    def _record_rejection_code(self, operation: SmallFileOperation, error_code: ErrorCode) -> None:
        """Record one closed registry code onto the rejection ring, if a member.

        The ring accepts no label outside its own closed vocabulary, so a
        code without a ring member keeps today's behavior with no record.
        """

        try:
            reason_code = SmallFileRejectionReason(error_code.value)
        except ValueError:
            return
        self.metrics.record_rejection(operation=operation, reason_code=reason_code)

    def _record_policy_rejection(
        self, operation: SmallFileOperation, error_code: ErrorCode
    ) -> None:
        """Record one closed policy-failure code into the rejection ring.

        Only the classified policy codes have ring members; a code outside
        that closed set keeps today's behavior with no ring record, because
        the ring accepts no label outside its own closed vocabulary.
        """

        reason_code = _POLICY_REJECTION_REASON_BY_CODE.get(error_code)
        if reason_code is None:
            return
        self.metrics.record_rejection(operation=operation, reason_code=reason_code)

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
