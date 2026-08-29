"""Framework-neutral orchestration of the resumable multipart upload (spec 4-6).

:class:`MultipartUploadService` owns the multipart session lifecycle over
injected ports only — the durable
:class:`~personal_os.multipart_upload.ports.MultipartSessionStore`, the
frozen bound-operation evidence of each session, the existing small-file
preflight/policy/publication seams, the bounded canonical object store,
the six-method staging provider capability and the aware UTC clock. The
exact ordered flows of the Child 7 spec are pinned here:

- ``create_or_resume`` (6.1) re-runs the policy guard and the update-base
  recheck, then reserves the operation and its session BEFORE any provider
  call (persist-before-create), mints the staging upload only when the
  reserved row carries no identity yet, and lands the fenced post-create
  identity write; the divergent-identity closed error aborts this caller's
  own fresh upload before propagating, and an exact replay never mints
  provider work twice.
- ``issue_part_url`` (6.2) rechecks ownership, state, part range and policy
  before presigning exactly one numbered part's byte range.
- ``status`` (6.1) reconciles the provider-observed completed parts into
  the durable row for a forward session and returns the frozen terminal
  evidence of a terminal one without any provider call.
- ``complete`` (6.3) claims the serialized completion lease, rechecks
  policy and base, proves every required part through ``ListParts``,
  completes the staging upload, full-verifies the staging bytes through
  the bounded canonical spool, publishes exactly once through the
  existing small-file publication gateway, freezes the terminal result,
  then requests the inline exact staging delete.
- ``abort`` terminalizes user cancellation into the exact cleanup
  obligation, and ``run_exact_cleanup`` (6.4) executes one bounded batch
  of exact obligations over only each session's persisted private
  identities.

Provider response loss is status/replay, never a duplicated publish: a
retryable typed failure after the claim leaves the row fenced for the
lease-expiry replay, while a provider-state-invalid observation or a
decided integrity/policy/conflict outcome lands its closed terminal
failure state together with the exact cleanup obligation. Every
``BaseException`` raised after provider work persists the exact cleanup
obligation before propagating, and the committed session's inline staging
delete surfaces its own closed reason on the rejection ring instead of
being swallowed.

The module imports no FastAPI, SQLAlchemy, R2 SDK or request type. The
staging key, provider upload ID, ETag, presigned URL, digest and raw bytes
never enter a typed error, a safe detail, a log line or a metric label:
the staging key crosses this module as the private ``str`` seam the
provider adapter re-validates, and the only reason text ever rendered is
the closed registry token.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import DiagnosticEventSink, EventName
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.exclusion_policy.enforcement import AllowedPolicyRevisionBinding
from personal_os.exclusion_policy.errors import ExclusionPolicyError, is_policy_system_failure
from personal_os.multipart_upload.contracts import (
    MultipartCompletionResult,
    MultipartPartGeometry,
    MultipartPartRange,
    MultipartPartUrl,
    MultipartSessionState,
    MultipartSessionStatus,
    MultipartUploadPlan,
    MultipartUploadSessionId,
)
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.metrics import (
    MultipartCleanupOutcome,
    MultipartCompletionOutcome,
    MultipartMetricFlow,
    MultipartRejectionReason,
    MultipartSessionOutcome,
    MultipartUploadMetrics,
)
from personal_os.multipart_upload.ports import (
    MultipartCleanupClaim,
    MultipartProviderPartETag,
    MultipartProviderUploadId,
    MultipartSessionClaim,
    MultipartSessionRecord,
    MultipartSessionStore,
)
from personal_os.object_storage import (
    CanonicalObjectStore,
    ExpectedObject,
    VerifiedObjectReceipt,
)
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.small_file_sync.contracts import (
    BoundSmallFileOperation,
    SmallFileDeviceContext,
    SmallFileOperation,
    SmallFilePreflight,
)
from personal_os.small_file_sync.ports import (
    AwareUtcClock,
    SmallFilePolicyGuard,
    SmallFilePublicationGateway,
    SmallFileUploadOperationStore,
)
from personal_os.small_file_sync.service import (
    derive_create_title,
    derive_source_type,
    terminal_result_from_publication,
)
from personal_os.source_locators.values import NormalizedLocator
from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    UpdateSourceVersion,
)
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.reading import (
    CanonicalReadStateError,
    CanonicalSourceReadStore,
    ReadCurrentSourceCommand,
)
from personal_os.sources.results import SourceVersionPublicationResult

#: The private staging-key prefix of the multipart staging grammar. The
#: derived key is ``staging/multipart/{opaque session ID}``: the session-ID
#: grammar (32 to 128 printable base64url characters) keeps the derived key
#: inside the provider's validated grammar, and the prefix keeps it a shape
#: no canonical ``objects/sha256/...`` key can satisfy.
#:
#: The R2 package holds a grammar twin (``_STAGING_KEY_PREFIX`` in
#: ``r2_object_storage.multipart``); the dependency direction forbids sharing
#: one constant, and drift between them fails closed at
#: ``MultipartStagingKey.parse`` on the provider boundary.
_STAGING_KEY_PREFIX: Final[str] = "staging/multipart/"

#: Object-storage failure codes that are the closed content-integrity
#: rejection of the staging verification spool (spec 6.3.4/8): the staged
#: bytes failed the bounded size or digest verification, so nothing may
#: publish. Every other object-storage failure keeps its availability
#: meaning through the retryable dependency-unavailable mapping.
_SPOOL_INTEGRITY_FAILURE_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
        ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED,
    }
)

#: The forward session states: the chain from creation toward the terminal
#: write. A create replay of a session outside this family — a landed
#: failure obligation or a terminal state — fails closed instead of handing
#: out a plan for provider work that will never run again.
_FORWARD_SESSION_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CREATED,
        MultipartSessionState.UPLOADING,
        MultipartSessionState.COMPLETING,
        MultipartSessionState.VERIFYING,
        MultipartSessionState.PROMOTING,
    }
)

#: The states in which one more part URL may be issued (spec 6.2): the
#: forward pre-completion states only — a session whose completion a
#: claimant holds, or whose failure obligation landed, issues nothing.
_PART_URL_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CREATED,
        MultipartSessionState.UPLOADING,
    }
)

#: The forward states whose completed parts ``status`` reconciles through
#: the provider observation (spec 6.1); every other state answers from the
#: persisted durable row without any provider call.
_RECONCILING_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CREATED,
        MultipartSessionState.UPLOADING,
    }
)

#: The closed failure-obligation states an ``abort`` replays idempotently:
#: the obligation already landed, so the safe status returns without a new
#: terminal write.
_FAILURE_OBLIGATION_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CANCELLING,
        MultipartSessionState.EXPIRED,
        MultipartSessionState.INTEGRITY_FAILED,
        MultipartSessionState.POLICY_DENIED,
        MultipartSessionState.CLEANUP_PENDING,
    }
)

#: The deterministic locator the frozen update evidence carries into its
#: policy recheck. The durable bound operation deliberately drops an
#: update's normalized locator (updates resolve by canonical source
#: identity), while the recheck preflight shape requires one; this fixed
#: non-empty single-segment value stands in so the guard's subject
#: evaluates on the exact workspace, source, media-type and size evidence —
#: the same locator-free subject evidence the publication boundary itself
#: evaluates. It is never a real vault path, never persisted and never a
#: label.
_RECHECK_UPDATE_LOCATOR: Final[NormalizedLocator] = NormalizedLocator(
    "multipart-update-session-evidence"
)


def derive_staging_key(session_id: MultipartUploadSessionId) -> str:
    """Derive one session's private staging-object key from its opaque ID.

    The derivation is deterministic and collision-free inside the session's
    own grammar; the value stays server-private database-sensitive material
    that crosses only the store/provider seam and is re-validated by the
    provider adapter before any SDK call.
    """

    return f"{_STAGING_KEY_PREFIX}{session_id.value}"


def reconstruct_recheck_preflight(bound: BoundSmallFileOperation) -> SmallFilePreflight:
    """Rebuild the frozen policy-recheck shape of one session's evidence.

    Every policy-relevant field — workspace-bound identity, event and
    idempotency key, the update's canonical source/base, the declared
    digest, exact size, media type and policy revision — is carried exactly
    from the durable bound operation. Two fields cannot cross the frozen
    evidence and are documented stand-ins instead of fabrications of
    device data: the device-local file identity (never policy evidence in
    any guard implementation) carries the durable operation identity, and
    an update's locator — which the bound evidence deliberately drops —
    carries the fixed :data:`_RECHECK_UPDATE_LOCATOR` stand-in. No guard
    implementation in this repository evaluates the ``local_file_id``
    field at all.
    """

    return SmallFilePreflight(
        event_id=bound.event_id,
        idempotency_key=bound.idempotency_key,
        operation=bound.operation,
        local_file_id=bound.operation_id,
        source_id=bound.update_source_id,
        base_version_id=bound.update_base_version_id,
        normalized_locator=(
            bound.normalized_locator
            if bound.normalized_locator is not None
            else _RECHECK_UPDATE_LOCATOR
        ),
        sha256=bound.declared_sha256,
        size_bytes=bound.declared_size_bytes,
        media_type=bound.declared_media_type,
        policy_revision_number=bound.policy_revision_number,
    )


@dataclass(frozen=True, slots=True)
class MultipartObservedPart:
    """One provider-observed completed part fact of a staging upload.

    The provider — never the client — observed the part number, its opaque
    ETag and its exact size (spec 3.6). The ETag is database-sensitive
    material that never renders outside a redacted ``repr``.
    """

    part_number: int
    etag: MultipartProviderPartETag
    size_bytes: int

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    def __post_init__(self) -> None:
        if self.part_number < 1:
            raise ValueError("part_number must be a positive part number")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative byte size")


@dataclass(frozen=True, slots=True)
class MultipartCleanupBatchOutcome:
    """The closed counters of one exact-cleanup batch execution."""

    cleaned_count: int
    failed_count: int


class MultipartStagingProvider(Protocol):
    """The six staging capabilities the orchestration drives (spec 6.1-6.4).

    The str staging-key seam is this module's private boundary: the
    composition root adapts it onto the validated provider key type and
    the bounded retry policy of the concrete provider adapter. Exactly one
    upload create, one-part presign, one-upload part listing, one-upload
    complete, one-upload abort and one-key staging-object removal exist —
    no listing, wildcard, prefix or canonical-key capability crosses here.
    """

    async def create_upload(self, staging_key: str) -> MultipartProviderUploadId: ...

    async def presign_part(
        self,
        staging_key: str,
        upload_id: MultipartProviderUploadId,
        part_range: MultipartPartRange,
    ) -> MultipartPartUrl: ...

    async def list_parts(
        self, staging_key: str, upload_id: MultipartProviderUploadId
    ) -> tuple[MultipartObservedPart, ...]: ...

    async def complete_upload(
        self,
        staging_key: str,
        upload_id: MultipartProviderUploadId,
        parts: Sequence[MultipartObservedPart],
    ) -> None: ...

    async def abort_upload(
        self, staging_key: str, upload_id: MultipartProviderUploadId
    ) -> None: ...

    async def delete_staging_object(self, staging_key: str) -> None: ...


class MultipartStagingByteSource(Protocol):
    """The bounded full-read stream of one session's staging object.

    The composition root binds the canonical object-storage reader over
    the exact private staging key; the stream the verification spool
    consumes is the provider-observed staging bytes only, never a client
    stream and never a canonical object.
    """

    def open_staging_stream(
        self, staging_key: str
    ) -> AbstractAsyncContextManager[AsyncIterable[bytes]]: ...


class MultipartSessionEvidenceStore(Protocol):
    """The frozen bound-operation evidence of each durable session."""

    async def load_bound_operation(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> BoundSmallFileOperation: ...


def _multipart_error(error_code: ErrorCode) -> MultipartUploadError:
    return MultipartUploadError(error_code)


def _geometry_of(record: MultipartSessionRecord) -> MultipartPartGeometry:
    """Rebuild the frozen geometry of one durable session record."""

    return MultipartPartGeometry(
        total_size_bytes=record.total_size_bytes,
        part_size_bytes=record.part_size_bytes,
        part_count=record.part_count,
    )


def _status_from_record(record: MultipartSessionRecord) -> MultipartSessionStatus:
    """Project one durable record into its safe observable status."""

    return MultipartSessionStatus(
        session_id=record.session_id,
        state=record.state,
        part_size_bytes=record.part_size_bytes,
        part_count=record.part_count,
        expires_at=record.expires_at,
        completed_part_numbers=record.completed_part_numbers,
        terminal_result=record.terminal_result,
    )


def _plan_from_record(record: MultipartSessionRecord) -> MultipartUploadPlan:
    """Project one durable record into its session-bound upload plan."""

    return MultipartUploadPlan(
        session_id=record.session_id,
        part_size_bytes=record.part_size_bytes,
        part_count=record.part_count,
        expires_at=record.expires_at,
    )


def _require_provider_identity(
    record: MultipartSessionRecord,
) -> tuple[str, MultipartProviderUploadId]:
    """Return the session's private staging identity, or fail closed."""

    staging_key = record.staging_key
    upload_id = record.provider_upload_id
    if staging_key is None or upload_id is None:
        raise _multipart_error(ErrorCode.MULTIPART_SESSION_STATE_INVALID)
    return (staging_key, upload_id)


def _validate_observed_parts(
    observed: Sequence[MultipartObservedPart],
    geometry: MultipartPartGeometry,
) -> tuple[MultipartObservedPart, ...]:
    """Prove the provider observation against the exact frozen geometry.

    Every required part number must appear exactly once with its exact
    window size, and nothing else may exist (spec 6.3.2): an absent,
    duplicate, extra or wrong-size part is the closed provider-state
    invalid observation that stops completion and schedules exact cleanup.
    """

    if len(observed) != geometry.part_count:
        raise _multipart_error(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID)
    by_part_number: dict[int, MultipartObservedPart] = {}
    for part in observed:
        if part.part_number in by_part_number:
            raise _multipart_error(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID)
        by_part_number[part.part_number] = part
    ordered: list[MultipartObservedPart] = []
    for part_number in range(1, geometry.part_count + 1):
        required_part = by_part_number.get(part_number)
        if required_part is None:
            raise _multipart_error(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID)
        if required_part.size_bytes != geometry.part_range(part_number).size_bytes:
            raise _multipart_error(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID)
        ordered.append(required_part)
    return tuple(ordered)


class _ExhaustedByteStream:
    """Already-exhausted fallback stream for the publication gateway call.

    The verification spool already stored and fully verified the staging
    bytes through the canonical object store, so the publication gateway's
    resolve-first content-addressable lookup hits and this stream is never
    consumed. Should the verified object vanish between the two steps, the
    gateway's own verification fails closed instead of publishing anything
    unverified.
    """

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration


_EXHAUSTED_BYTE_STREAM: Final[AsyncIterable[bytes]] = _ExhaustedByteStream()


@dataclass(slots=True)
class MultipartUploadService:
    """Orchestrates the resumable multipart staging transfer (spec 4-6).

    Depends only on provider-neutral ports: the durable session store, the
    frozen session evidence, the small-file operation store, policy guard,
    current-source resolver, publication gateway and canonical object
    store, the six-method staging provider, the staging byte source, the
    closed metrics sink and the aware UTC clock. Lease fencing, expiry
    strikes, state transitions and cleanup obligations are the store's own
    authority: this service never re-derives them, never opens a store
    transaction around a provider call, and never masks the store's closed
    errors.
    """

    session_store: MultipartSessionStore
    evidence_store: MultipartSessionEvidenceStore
    operation_store: SmallFileUploadOperationStore
    policy_guard: SmallFilePolicyGuard
    current_sources: CanonicalSourceReadStore
    publication_gateway: SmallFilePublicationGateway
    object_store: CanonicalObjectStore
    staging_provider: MultipartStagingProvider
    staging_byte_source: MultipartStagingByteSource
    metrics: MultipartUploadMetrics
    clock: AwareUtcClock
    diagnostics: DiagnosticEventSink | None = None

    async def create_or_resume(
        self,
        *,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartUploadPlan:
        """Create or exactly replay the one session of a frozen operation."""

        started_at = self.clock()
        try:
            return await self._create_or_resume_once(
                preflight=preflight,
                device_context=device_context,
                diagnostic_context=diagnostic_context,
                started_at=started_at,
            )
        except ApplicationError as error:
            self._record_rejection_code(MultipartMetricFlow.SESSION_CREATE, error.error_code)
            raise

    async def status(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionStatus:
        """Return the safe observable state, reconciling forward sessions."""

        try:
            record = await self.session_store.load_owned_session(
                session_id=session_id,
                device_context=device_context,
                diagnostic_context=diagnostic_context,
            )
            if record.state in _RECONCILING_STATES:
                record = await self._reconcile_provider_parts(
                    record=record,
                    device_context=device_context,
                    diagnostic_context=diagnostic_context,
                )
            return _status_from_record(record)
        except ApplicationError as error:
            self._record_rejection_code(MultipartMetricFlow.SESSION_STATUS, error.error_code)
            raise

    async def issue_part_url(
        self,
        *,
        session_id: MultipartUploadSessionId,
        part_number: int,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartPartUrl:
        """Authorize exactly one numbered part's byte range for one PUT."""

        try:
            return await self._issue_part_url_once(
                session_id=session_id,
                part_number=part_number,
                device_context=device_context,
                diagnostic_context=diagnostic_context,
            )
        except ApplicationError as error:
            self._record_rejection_code(MultipartMetricFlow.PART_URL, error.error_code)
            raise

    async def complete(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartCompletionResult:
        """Claim and run the serialized completion of one session."""

        started_at = self.clock()
        try:
            return await self._complete_once(
                session_id=session_id,
                device_context=device_context,
                diagnostic_context=diagnostic_context,
                started_at=started_at,
            )
        except ApplicationError as error:
            self._record_rejection_code(MultipartMetricFlow.COMPLETION, error.error_code)
            raise

    async def abort(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionStatus:
        """Terminalize user cancellation into the exact cleanup obligation."""

        try:
            return await self._abort_once(
                session_id=session_id,
                device_context=device_context,
                diagnostic_context=diagnostic_context,
            )
        except ApplicationError as error:
            self._record_rejection_code(MultipartMetricFlow.SESSION_ABORT, error.error_code)
            raise

    async def run_exact_cleanup(
        self,
        *,
        batch_limit: int,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartCleanupBatchOutcome:
        """Execute one bounded batch of exact cleanup obligations (spec 6.4).

        Every claim carries only its session's persisted private exact
        resource identities: the incomplete provider upload is aborted for
        its exact upload ID and the staging object is removed for its exact
        key, both idempotent in the provider's absence rule; an expired
        session that never landed an identity has nothing to touch and is
        trivially cleaned. No list, wildcard, prefix or canonical-object
        capability is reachable from here.
        """

        claims = await self.session_store.claim_cleanup_batch(
            batch_limit=batch_limit, diagnostic_context=diagnostic_context
        )
        cleaned_count = 0
        failed_count = 0
        for claim in claims:
            is_succeeded, failure_reason = await self._execute_exact_cleanup(
                claim=claim, diagnostic_context=diagnostic_context
            )
            try:
                await self.session_store.record_cleanup_result(
                    claim=claim,
                    is_succeeded=is_succeeded,
                    failure_reason=failure_reason,
                    diagnostic_context=diagnostic_context,
                )
            except ApplicationError as error:
                # The lease-fenced outcome write refused: the obligation
                # stays with the row's own lease state; the refused token
                # surfaces on the ring instead of being swallowed.
                self._record_rejection_code(MultipartMetricFlow.CLEANUP, error.error_code)
                failed_count += 1
                continue
            if is_succeeded:
                cleaned_count += 1
                self.metrics.record_cleanup(outcome=MultipartCleanupOutcome.CLEANED)
            else:
                failed_count += 1
                self.metrics.record_cleanup(outcome=MultipartCleanupOutcome.FAILED)
                if failure_reason is not None:
                    self._record_rejection_code(MultipartMetricFlow.CLEANUP, failure_reason)
        return MultipartCleanupBatchOutcome(cleaned_count=cleaned_count, failed_count=failed_count)

    # --- create-or-resume (spec 6.1) ---------------------------------------

    async def _create_or_resume_once(
        self,
        *,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
        started_at: datetime,
    ) -> MultipartUploadPlan:
        # Policy first (spec 7): a denial answers before any store or
        # provider access; a policy SYSTEM failure propagates as its own
        # typed error for the closed 409/503 envelope.
        policy_binding = await self._authorize_policy(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=diagnostic_context,
        )
        # The update base must still be current: a stale or missing base is
        # the existing safe conflict token, before any reservation.
        await self._recheck_update_base(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=diagnostic_context,
        )
        operation = await self.operation_store.reserve_operation(
            preflight=preflight,
            device_context=device_context,
            policy_binding=policy_binding,
            diagnostic_context=diagnostic_context,
        )
        # Persist-before-create (spec 6.1): the reserved row is the durable
        # recovery state that makes an ambiguous provider create retryable.
        record = await self.session_store.reserve_session(
            operation=operation,
            device_context=device_context,
            diagnostic_context=diagnostic_context,
        )
        if record.staging_key is not None and record.provider_upload_id is not None:
            # Exact replay: the row already carries its provider identity,
            # so no second provider workload is minted. A session whose
            # failure obligation or terminal state already landed replays
            # no plan at all — its safe evidence is the frozen status.
            if record.state not in _FORWARD_SESSION_STATES:
                raise _multipart_error(ErrorCode.MULTIPART_SESSION_STATE_INVALID)
            self.metrics.record_session(
                outcome=MultipartSessionOutcome.REPLAYED,
                duration_seconds=self._elapsed_seconds_since(started_at),
            )
            return _plan_from_record(record)
        staging_key = derive_staging_key(record.session_id)
        upload_id = await self.staging_provider.create_upload(staging_key)
        try:
            record = await self.session_store.record_provider_identity(
                session_id=record.session_id,
                staging_key=staging_key,
                provider_upload_id=upload_id,
                device_context=device_context,
                diagnostic_context=diagnostic_context,
            )
        except BaseException:
            # The fenced identity write refused: this caller minted the
            # fresh upload, so it owns aborting its own orphan before the
            # closed reason propagates.
            await self._abort_fresh_upload_best_effort(
                staging_key=staging_key,
                upload_id=upload_id,
                flow=MultipartMetricFlow.SESSION_CREATE,
            )
            raise
        self.metrics.record_session(
            outcome=MultipartSessionOutcome.CREATED,
            duration_seconds=self._elapsed_seconds_since(started_at),
        )
        return _plan_from_record(record)

    # --- part URL issuance (spec 6.2) ---------------------------------------

    async def _issue_part_url_once(
        self,
        *,
        session_id: MultipartUploadSessionId,
        part_number: int,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartPartUrl:
        record = await self.session_store.load_owned_session(
            session_id=session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context,
        )
        if record.state not in _PART_URL_STATES:
            raise _multipart_error(ErrorCode.MULTIPART_SESSION_STATE_INVALID)
        try:
            part_range = _geometry_of(record).part_range(part_number)
        except ValueError:
            raise _multipart_error(ErrorCode.MULTIPART_PART_INVALID) from None
        staging_key, upload_id = _require_provider_identity(record)
        bound = await self.evidence_store.load_bound_operation(
            session_id=session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context,
        )
        # Policy recheck on the frozen evidence: an advance to deny blocks
        # every new URL (spec 7).
        await self._authorize_policy(
            preflight=reconstruct_recheck_preflight(bound),
            device_context=device_context,
            diagnostic_context=diagnostic_context,
        )
        return await self.staging_provider.presign_part(staging_key, upload_id, part_range)

    # --- status reconciliation (spec 6.1) -----------------------------------

    async def _reconcile_provider_parts(
        self,
        *,
        record: MultipartSessionRecord,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionRecord:
        staging_key, upload_id = _require_provider_identity(record)
        observed = await self.staging_provider.list_parts(staging_key, upload_id)
        geometry = _geometry_of(record)
        # Reconcile only the numbered completed parts that fit the exact
        # geometry (spec 6.1): partial progress is the normal resume shape,
        # while a number outside the geometry or a wrong-size window is the
        # closed provider-state observation that reconciles nothing.
        for part in observed:
            if not 1 <= part.part_number <= geometry.part_count:
                raise _multipart_error(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID)
            if part.size_bytes != geometry.part_range(part.part_number).size_bytes:
                raise _multipart_error(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID)
            await self.session_store.record_provider_part(
                session_id=record.session_id,
                part_number=part.part_number,
                etag=part.etag,
                verified_size_bytes=part.size_bytes,
                device_context=device_context,
                diagnostic_context=diagnostic_context,
            )
        return await self.session_store.load_owned_session(
            session_id=record.session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context,
        )

    # --- completion (spec 6.3) ----------------------------------------------

    async def _complete_once(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
        started_at: datetime,
    ) -> MultipartCompletionResult:
        claim = await self.session_store.claim_completion(
            session_id=session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context,
        )
        if claim.is_committed_replay:
            # Response-loss replay: the frozen terminal result returns
            # unchanged and nothing re-publishes (spec 6.3/8).
            self.metrics.record_completion(
                outcome=MultipartCompletionOutcome.REPLAYED,
                duration_seconds=self._elapsed_seconds_since(started_at),
            )
            frozen = claim.session.terminal_result
            if frozen is None:  # pragma: no cover - store contract guarantees
                raise _multipart_error(ErrorCode.MULTIPART_SESSION_STATE_INVALID)
            return MultipartCompletionResult(
                state=MultipartSessionState.COMMITTED, terminal_result=frozen
            )
        return await self._complete_under_claim(
            claim=claim,
            device_context=device_context,
            diagnostic_context=diagnostic_context,
            started_at=started_at,
        )

    async def _complete_under_claim(
        self,
        *,
        claim: MultipartSessionClaim,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
        started_at: datetime,
    ) -> MultipartCompletionResult:
        session = claim.session
        session_id = session.session_id
        bound = await self.evidence_store.load_bound_operation(
            session_id=session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context,
        )
        # Claimant rechecks (spec 6.3.1) before any provider work: policy
        # on the frozen evidence, then the update base.
        recheck_preflight = reconstruct_recheck_preflight(bound)
        try:
            await self._authorize_policy(
                preflight=recheck_preflight,
                device_context=device_context,
                diagnostic_context=diagnostic_context,
            )
            await self._recheck_update_base(
                preflight=recheck_preflight,
                device_context=device_context,
                diagnostic_context=diagnostic_context,
            )
        except MultipartUploadError as error:
            if error.error_code is ErrorCode.MULTIPART_POLICY_DENIED:
                await self._land_failure(
                    claim=claim,
                    failure_state=MultipartSessionState.POLICY_DENIED,
                    outcome=MultipartCompletionOutcome.POLICY_DENIED,
                    started_at=started_at,
                    diagnostic_context=diagnostic_context,
                )
            raise
        except SourcePublicationError as error:
            if error.error_code is ErrorCode.SOURCE_VERSION_CONFLICT:
                await self._land_failure(
                    claim=claim,
                    failure_state=MultipartSessionState.CANCELLING,
                    outcome=MultipartCompletionOutcome.CONFLICT,
                    started_at=started_at,
                    diagnostic_context=diagnostic_context,
                )
            raise
        staging_key, upload_id = _require_provider_identity(session)
        geometry = _geometry_of(session)
        # ListParts proof (spec 6.3.2): every provider failure below leaves
        # the claim fenced for the lease-expiry replay unless it is the
        # closed provider-state observation, which stops completion and
        # schedules the exact cleanup.
        try:
            observed = await self.staging_provider.list_parts(staging_key, upload_id)
        except ApplicationError as error:
            await self._land_provider_state_failure(
                claim=claim,
                error=error,
                started_at=started_at,
                diagnostic_context=diagnostic_context,
            )
            raise
        try:
            ordered = _validate_observed_parts(observed, geometry)
        except MultipartUploadError:
            await self._land_failure(
                claim=claim,
                failure_state=MultipartSessionState.INTEGRITY_FAILED,
                outcome=MultipartCompletionOutcome.INTEGRITY_FAILED,
                started_at=started_at,
                diagnostic_context=diagnostic_context,
            )
            raise
        try:
            await self.staging_provider.complete_upload(staging_key, upload_id, ordered)
        except ApplicationError as error:
            # A lost or failed complete response is status/replay, never a
            # duplicated publish: the claim stays fenced for the retry.
            await self._land_provider_state_failure(
                claim=claim,
                error=error,
                started_at=started_at,
                diagnostic_context=diagnostic_context,
            )
            raise
        # Provider work is done: from here every BaseException persists the
        # exact cleanup obligation before propagating.
        try:
            await self._verify_staging_object(
                staging_key=staging_key,
                bound=bound,
                diagnostic_context=diagnostic_context,
            )
            publication = await self._publish(
                bound=bound,
                device_context=device_context,
                diagnostic_context=diagnostic_context,
            )
        except ApplicationError as error:
            classification = self._classify_claim_failure(error)
            if classification is not None:
                failure_state, outcome = classification
                await self._land_failure(
                    claim=claim,
                    failure_state=failure_state,
                    outcome=outcome,
                    started_at=started_at,
                    diagnostic_context=diagnostic_context,
                )
            raise
        except BaseException:
            await self._land_failure(
                claim=claim,
                failure_state=MultipartSessionState.CANCELLING,
                outcome=MultipartCompletionOutcome.REJECTED,
                started_at=started_at,
                diagnostic_context=diagnostic_context,
            )
            raise
        terminal = terminal_result_from_publication(publication)
        await self.session_store.record_terminal_result(
            claim=claim, result=terminal, diagnostic_context=diagnostic_context
        )
        self.metrics.record_completion(
            outcome=MultipartCompletionOutcome.COMMITTED,
            duration_seconds=self._elapsed_seconds_since(started_at),
        )
        await self._delete_staging_after_commit(
            staging_key=staging_key, diagnostic_context=diagnostic_context
        )
        return MultipartCompletionResult(
            state=MultipartSessionState.COMMITTED, terminal_result=terminal
        )

    async def _verify_staging_object(
        self,
        *,
        staging_key: str,
        bound: BoundSmallFileOperation,
        diagnostic_context: DiagnosticContext,
    ) -> VerifiedObjectReceipt:
        """Full-read the staging object through the bounded verification spool.

        The staging stream is spooled through the canonical object store's
        bounded full-verification path: exact size, full SHA-256 and media
        type are proved before anything may publish (spec 6.3.4/5). An
        integrity rejection maps to the closed multipart integrity token;
        any other object-store failure is the retryable dependency outage
        with its cause chained, never a masked reason.
        """

        del diagnostic_context
        try:
            async with self.staging_byte_source.open_staging_stream(staging_key) as stream:
                return await self.object_store.store_stream(
                    stream,
                    bound.declared_size_bytes,
                    bound.declared_media_type.value,
                    bound.declared_sha256.hexadecimal,
                )
        except ObjectStorageError as error:
            if error.error_code in _SPOOL_INTEGRITY_FAILURE_CODES:
                raise _multipart_error(ErrorCode.MULTIPART_INTEGRITY_FAILED) from error
            raise _multipart_error(ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE) from error

    async def _publish(
        self,
        *,
        bound: BoundSmallFileOperation,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        """Publish exactly once through the existing small-file gateway.

        The publication command binds the session's frozen evidence — the
        credential-derived workspace and device actor, the journal event
        and idempotency key, the declared fingerprint as the expected
        object — and the gateway's own guarded path rechecks policy and
        base inside the publication transaction (spec 6.3.6). The verified
        object resolves content-addressably, so the exhausted fallback
        stream is never consumed.
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
                raise _multipart_error(ErrorCode.MULTIPART_SESSION_STATE_INVALID)
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
                raise _multipart_error(ErrorCode.MULTIPART_SESSION_STATE_INVALID)
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
            raise _multipart_error(ErrorCode.MULTIPART_SESSION_STATE_INVALID)
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
            raise _multipart_error(ErrorCode.MULTIPART_SESSION_STATE_INVALID)
        return await self.publication_gateway.publish_update(
            command=update_command,
            stream=_EXHAUSTED_BYTE_STREAM,
            policy_binding=policy_binding,
            bound_operation=bound,
            diagnostic_context=diagnostic_context,
        )

    def _classify_claim_failure(
        self, error: ApplicationError
    ) -> tuple[MultipartSessionState, MultipartCompletionOutcome] | None:
        """Classify one typed post-complete failure into its terminal write.

        ``None`` keeps the claim fenced for the lease-expiry replay — the
        retryable dependency and concurrency failures whose response loss
        is status/replay, never a duplicated publish. A decided integrity
        failure, the no-candidate conflict and every other non-retryable
        typed rejection land their closed failure obligation.
        """

        if (
            isinstance(error, MultipartUploadError)
            and error.error_code is ErrorCode.MULTIPART_INTEGRITY_FAILED
        ):
            return (
                MultipartSessionState.INTEGRITY_FAILED,
                MultipartCompletionOutcome.INTEGRITY_FAILED,
            )
        if (
            isinstance(error, SourcePublicationError)
            and error.error_code is ErrorCode.SOURCE_VERSION_CONFLICT
        ):
            # A stale base at any required recheck is the terminal
            # no-candidate conflict outcome (spec 6.3): this child retains
            # no verified candidate, so the obligation is exact cleanup.
            return (MultipartSessionState.CANCELLING, MultipartCompletionOutcome.CONFLICT)
        if not error.is_retryable:
            return (MultipartSessionState.CANCELLING, MultipartCompletionOutcome.REJECTED)
        return None

    async def _land_provider_state_failure(
        self,
        *,
        claim: MultipartSessionClaim,
        error: ApplicationError,
        started_at: datetime,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        """Land the provider-state observation's integrity failure, if it is one."""

        if (
            isinstance(error, MultipartUploadError)
            and error.error_code is ErrorCode.MULTIPART_PROVIDER_STATE_INVALID
        ):
            await self._land_failure(
                claim=claim,
                failure_state=MultipartSessionState.INTEGRITY_FAILED,
                outcome=MultipartCompletionOutcome.INTEGRITY_FAILED,
                started_at=started_at,
                diagnostic_context=diagnostic_context,
            )

    async def _land_failure(
        self,
        *,
        claim: MultipartSessionClaim,
        failure_state: MultipartSessionState,
        outcome: MultipartCompletionOutcome,
        started_at: datetime,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        """Persist one closed failure obligation under the fenced claim.

        The store's compare-and-set lands the failure state together with
        its exact cleanup obligation and releases the lease. When the
        guarded write itself refuses — a lease already replaced — the
        original failure still surfaces unchanged and the refused token is
        recorded on the rejection ring instead of being swallowed.
        """

        try:
            await self.session_store.record_terminal_result(
                claim=claim,
                failure_state=failure_state,
                diagnostic_context=diagnostic_context,
            )
        except ApplicationError as write_error:
            self._record_rejection_code(MultipartMetricFlow.COMPLETION, write_error.error_code)
        self.metrics.record_completion(
            outcome=outcome, duration_seconds=self._elapsed_seconds_since(started_at)
        )

    async def _delete_staging_after_commit(
        self,
        *,
        staging_key: str,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        """Run the committed session's inline exact staging delete (spec 6.3.7).

        The committed terminal state has no cleanup-obligation exit, so a
        failed inline delete never fails the frozen result: its closed
        reason token surfaces on the rejection ring — the readable reason
        surface for this one path — and the expiry sweep keeps no second
        obligation for it.
        """

        del diagnostic_context
        try:
            await self.staging_provider.delete_staging_object(staging_key)
        except ApplicationError as error:
            self._record_rejection_code(MultipartMetricFlow.COMPLETION, error.error_code)
        except BaseException:
            self._record_rejection(
                MultipartMetricFlow.COMPLETION, MultipartRejectionReason.MULTIPART_CLEANUP_FAILED
            )

    # --- cancellation (spec 6.4) ---------------------------------------------

    async def _abort_once(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionStatus:
        record = await self.session_store.load_owned_session(
            session_id=session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context,
        )
        if record.state in _FAILURE_OBLIGATION_STATES:
            # Idempotent replay of a landed obligation: the safe status
            # returns without a second terminal write or provider call.
            return _status_from_record(record)
        if record.state not in _PART_URL_STATES:
            # A committed or cleaned terminal, or a session whose
            # completion a claimant holds, cannot be user-cancelled here;
            # the expiry sweep resolves an abandoned completion.
            raise _multipart_error(ErrorCode.MULTIPART_SESSION_STATE_INVALID)
        claim = await self.session_store.claim_completion(
            session_id=session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context,
        )
        await self.session_store.record_terminal_result(
            claim=claim,
            failure_state=MultipartSessionState.CANCELLING,
            diagnostic_context=diagnostic_context,
        )
        cancelled = await self.session_store.load_owned_session(
            session_id=session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context,
        )
        return _status_from_record(cancelled)

    # --- exact cleanup (spec 6.4) ---------------------------------------------

    async def _execute_exact_cleanup(
        self,
        *,
        claim: MultipartCleanupClaim,
        diagnostic_context: DiagnosticContext,
    ) -> tuple[bool, ErrorCode | None]:
        """Touch only the claim's persisted exact resource identities."""

        del diagnostic_context
        session = claim.session
        staging_key = session.staging_key
        if staging_key is None:
            # A session that expired before its provider create has
            # nothing to abort or remove: trivially successful cleanup.
            return (True, None)
        try:
            upload_id = session.provider_upload_id
            if upload_id is not None:
                await self.staging_provider.abort_upload(staging_key, upload_id)
            await self.staging_provider.delete_staging_object(staging_key)
        except ApplicationError as error:
            self._record_rejection_code(MultipartMetricFlow.CLEANUP, error.error_code)
            return (False, error.error_code)
        except BaseException:
            # An untyped failure keeps the closed cleanup token as its
            # readable reason on the ring and the durable closed log,
            # records the failed obligation and never aborts the remaining
            # batch.
            self._record_rejection(
                MultipartMetricFlow.CLEANUP, MultipartRejectionReason.MULTIPART_CLEANUP_FAILED
            )
            return (False, ErrorCode.MULTIPART_CLEANUP_FAILED)
        return (True, None)

    async def _abort_fresh_upload_best_effort(
        self,
        *,
        staging_key: str,
        upload_id: MultipartProviderUploadId,
        flow: MultipartMetricFlow,
    ) -> None:
        """Abort this caller's own fresh orphan upload, best effort.

        The abort failure never masks the identity-write refusal that
        triggered it: its own closed reason lands on the rejection ring,
        and the original error keeps propagating unchanged.
        """

        try:
            await self.staging_provider.abort_upload(staging_key, upload_id)
        except ApplicationError as error:
            self._record_rejection_code(flow, error.error_code)
        except BaseException:
            self._record_rejection(flow, MultipartRejectionReason.MULTIPART_CLEANUP_FAILED)

    # --- shared rechecks -------------------------------------------------------

    async def _authorize_policy(
        self,
        *,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> AllowedPolicyRevisionBinding:
        """Re-evaluate the active exclusion policy at this boundary.

        A definite denial or indeterminate outcome is the closed multipart
        policy token; a policy SYSTEM failure (no active signed policy,
        corrupt signing material) propagates as its own typed error for
        the closed 409/503 envelope instead of collapsing a broken policy
        system into a denial.
        """

        try:
            return await self.policy_guard.authorize_small_file(
                preflight, device_context, diagnostic_context
            )
        except ExclusionPolicyError as error:
            if is_policy_system_failure(error):
                raise
            raise _multipart_error(ErrorCode.MULTIPART_POLICY_DENIED) from error

    async def _recheck_update_base(
        self,
        *,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        """Resolve the update base; a stale or missing base is the conflict token.

        The existing safe conflict token is reused unchanged — the client
        re-runs its own conflict path on a fresh event — and nothing is
        reserved or published for a stale base.
        """

        if preflight.operation is not SmallFileOperation.UPDATE:
            return
        update_source_id = preflight.source_id
        update_base_version_id = preflight.base_version_id
        if update_source_id is None or update_base_version_id is None:
            # Unreachable for validated evidence; keeps the path total.
            raise _multipart_error(ErrorCode.MULTIPART_SESSION_STATE_INVALID)
        try:
            reference = await self.current_sources.resolve_current(
                ReadCurrentSourceCommand(
                    workspace_id=device_context.workspace_id, source_id=update_source_id
                ),
                diagnostic_context,
            )
        except CanonicalReadStateError as error:
            raise SourcePublicationError(ErrorCode.SOURCE_VERSION_CONFLICT) from error
        except ExclusionPolicyError as error:
            if is_policy_system_failure(error):
                raise
            raise _multipart_error(ErrorCode.MULTIPART_POLICY_DENIED) from error
        if reference.source_version_id != update_base_version_id:
            raise SourcePublicationError(ErrorCode.SOURCE_VERSION_CONFLICT)

    def _record_rejection_code(self, flow: MultipartMetricFlow, error_code: ErrorCode) -> None:
        """Record one closed registry code onto the rejection ring, if a member.

        The ring accepts no label outside its own closed vocabulary, so a
        code without a ring member (for example the reused source conflict
        token) keeps today's behavior with no record — its readable
        surface is the typed error itself.
        """

        try:
            reason_code = MultipartRejectionReason(error_code.value)
        except ValueError:
            return
        self._record_rejection(flow, reason_code)

    def _record_rejection(
        self, flow: MultipartMetricFlow, reason_code: MultipartRejectionReason
    ) -> None:
        """Record one closed rejection on the ring and the durable closed log.

        The in-memory ring serves the process-local diagnostics snapshot;
        the structured closed event through the diagnostics sink is the
        durable surface — the rotating log an operator reads after a
        restart — carrying only the closed flow and reason tokens, never a
        key, URL, provider identity or digest (docs/15 observability).
        """

        self.metrics.record_rejection(flow=flow, reason_code=reason_code)
        if self.diagnostics is not None:
            self.diagnostics.emit(
                EventName.MULTIPART_UPLOAD_REJECTED,
                {"flow": flow, "reason": reason_code},
            )

    def _elapsed_seconds_since(self, started_at: datetime) -> float:
        # Clamped at zero so a clock seam that repeats or drifts backwards
        # can never turn a recorded duration negative.
        return max((self.clock() - started_at).total_seconds(), 0.0)


__all__ = [
    "MultipartCleanupBatchOutcome",
    "MultipartObservedPart",
    "MultipartSessionEvidenceStore",
    "MultipartStagingByteSource",
    "MultipartStagingProvider",
    "MultipartUploadService",
    "derive_staging_key",
    "reconstruct_recheck_preflight",
]
