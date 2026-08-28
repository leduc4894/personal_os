"""Provider-neutral ports for the multipart upload orchestration (spec 4-6).

The seam the later tasks build on: the application-facing service protocol
(create-or-resume, one part-URL issuance, completion), the durable
:class:`MultipartSessionStore` port the PostgreSQL adapter implements, plus
the provider identity value objects that stay private to this module — the
durable store and the staging provider adapters exchange them, but they are
deliberately absent from the package's public surface, never rendered
outside a redacted ``repr`` and never carried by a plan, status, URL or
error. The protocols import no FastAPI, SQLAlchemy, R2 SDK or request type;
device and workspace identity arrive only through the credential-derived
:class:`~personal_os.small_file_sync.contracts.SmallFileDeviceContext`, and
the server-owned
:class:`~personal_os.diagnostics.context.DiagnosticContext` travels with
every call for correlation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.multipart_upload.contracts import (
    MultipartCompletionResult,
    MultipartPartUrl,
    MultipartSessionState,
    MultipartUploadPlan,
    MultipartUploadSessionId,
)
from personal_os.small_file_sync.contracts import (
    SmallFileDeviceContext,
    SmallFilePreflight,
    SmallFileTerminalResult,
    SmallFileUploadOperation,
    UploadOperationToken,
)

#: Bounded length of one provider-assigned opaque identity value.
_MAX_PROVIDER_IDENTITY_LENGTH: Final[int] = 1024


@dataclass(frozen=True, slots=True)
class MultipartProviderUploadId:
    """The provider-assigned upload ID of one exact staging upload.

    Server-private database-sensitive material (spec 4.1): it exists only
    inside the store and staging provider boundary, is never returned to the
    plugin or public API and never renders outside a redacted ``repr``.
    """

    value: str

    def __repr__(self) -> str:
        return f"{type(self).__name__}(value=<redacted>)"

    def __post_init__(self) -> None:
        if not 1 <= len(self.value) <= _MAX_PROVIDER_IDENTITY_LENGTH:
            raise ValueError(
                f"provider upload ID must be 1 to {_MAX_PROVIDER_IDENTITY_LENGTH} characters long"
            )


@dataclass(frozen=True, slots=True)
class MultipartProviderPartETag:
    """The provider-observed ETag of one completed staging part.

    Part completion is proved by the provider (spec 3.6): the server obtains
    this value itself and stores it in PostgreSQL as database-sensitive
    material — the client never echoes one as trusted completion evidence,
    and it never renders outside a redacted ``repr``.
    """

    value: str

    def __repr__(self) -> str:
        return f"{type(self).__name__}(value=<redacted>)"

    def __post_init__(self) -> None:
        if not 1 <= len(self.value) <= _MAX_PROVIDER_IDENTITY_LENGTH:
            raise ValueError(
                f"provider part ETag must be 1 to {_MAX_PROVIDER_IDENTITY_LENGTH} characters long"
            )


@dataclass(frozen=True, slots=True)
class SealedMultipartOperationToken:
    """One reservation's raw operation token as AEAD-sealed row material.

    The session row carries the sealed preimage of its frozen operation's
    token hash so a completion claimant — potentially hours later, in another
    process — can rebuild the bound operation the publication fence needs.
    ``nonce`` and ``ciphertext`` are opaque sealed text (secret-bearing, never
    rendered); ``key_id`` names the versioned keyring key that sealed them so
    a previous-key seal stays openable until its re-seal.
    """

    key_id: str
    nonce: str = field(repr=False)
    ciphertext: str = field(repr=False)


class MultipartOperationTokenCodecPort(Protocol):
    """AEAD seam over the versioned keyring for the sealed operation token.

    ``seal_token`` always uses the current key; ``open_token`` resolves the
    key ID the row references. A decrypt or parameter failure fails closed as
    the safe typed error of the implementing boundary without crypto text.
    The codec is a serve-graph capability: the cleanup worker composes the
    durable store without it.
    """

    def current_key_id(self) -> str: ...

    def seal_token(self, *, token: UploadOperationToken) -> SealedMultipartOperationToken: ...

    def open_token(self, *, sealed: SealedMultipartOperationToken) -> UploadOperationToken: ...


class MultipartUploadApplicationService(Protocol):
    """The multipart upload use cases the API routes drive (spec 5/6).

    ``create_or_resume`` returns the single session bound to one frozen
    preflight operation — an exact replay resolves the existing session and
    never mints provider work twice. ``issue_part_url`` rechecks authority,
    policy, state and expiry, then authorizes exactly one numbered part's
    byte range for at most ten minutes. ``complete`` claims the serialized
    completion and returns either the persisted in-progress state or the
    frozen terminal source-event result. Implementations surface every
    closed failure through the typed
    :class:`~personal_os.multipart_upload.errors.MultipartUploadError`.
    """

    async def create_or_resume(
        self,
        *,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartUploadPlan: ...

    async def issue_part_url(
        self,
        *,
        session_id: MultipartUploadSessionId,
        part_number: int,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartPartUrl: ...

    async def complete(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartCompletionResult: ...


#: The closed fenced terminal failure obligations of spec 4.2 that a
#: completion claimant may land through the session store: user-initiated
#: cancellation, decided integrity failure and the re-checked policy
#: denial. Every active state may exit to each of them, and each resolves
#: into the exact staging cleanup obligation. Expiry is deliberately
#: absent: the 24-hour strike belongs to the expiry sweep, never to a
#: claimant's terminal write.
MULTIPART_TERMINAL_FAILURE_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CANCELLING,
        MultipartSessionState.INTEGRITY_FAILED,
        MultipartSessionState.POLICY_DENIED,
    }
)


@dataclass(frozen=True, slots=True)
class MultipartSessionRecord:
    """The durable store's private hydrated view of one session (spec 4.1).

    Carries the opaque public session identity, the current server state,
    the exact frozen geometry, the 24-hour deadline, the reconciled
    completed part numbers and — only for a committed session — its frozen
    terminal source-event result. ``staging_key`` and
    ``provider_upload_id`` are private provider identity exchanged only
    between the session store and the staging provider adapters: they are
    ``None`` on a session reserved before its provider create (spec 6.1
    persist-before-create) and land exclusively through the fenced
    post-create identity write; this view stays off the package's public
    surface, never renders outside a redacted ``repr`` and never crosses a
    plan, status, URL or error.
    """

    session_id: MultipartUploadSessionId
    state: MultipartSessionState
    part_size_bytes: int
    part_count: int
    total_size_bytes: int
    expires_at: datetime
    staging_key: str | None
    provider_upload_id: MultipartProviderUploadId | None
    completed_part_numbers: frozenset[int]
    terminal_result: SmallFileTerminalResult | None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"


@dataclass(frozen=True, slots=True)
class MultipartSessionClaim:
    """One durable completion claim handle over a session (spec 4.2).

    ``claim_token`` is the compare-and-set fence of every terminal write:
    a claimant holds the serialized completion only while the row carries
    the same token with an unexpired lease. A claim whose token is
    ``None`` is the committed replay shape — the session already froze its
    terminal result and no new provider work may be minted, so the frozen
    result rides on the record instead of a lease.
    """

    session: MultipartSessionRecord
    claim_token: UUID | None
    claim_expires_at: datetime | None

    @property
    def is_committed_replay(self) -> bool:
        """Report whether this claim is the frozen committed replay shape."""

        return self.claim_token is None


@dataclass(frozen=True, slots=True)
class MultipartCleanupClaim:
    """One database-leased exact cleanup claim over an obligated session.

    The claim carries the private exact resource identities the cleanup
    executor is permitted to touch — the staging key and the provider
    upload ID of exactly this session — plus the lease token that fences
    the cleanup result write. No list, prefix or wildcard authority ever
    derives from it.
    """

    session: MultipartSessionRecord
    claim_token: UUID
    claim_expires_at: datetime


class MultipartSessionStore(Protocol):
    """Durable multipart session store port: replay, fencing and cleanup.

    ``reserve_session`` lands the canonical session row for one frozen
    preflight-bound operation BEFORE any provider call (spec 6.1): the row
    carries no provider identity yet — that is the durable recovery state
    that makes an ambiguous create retryable — and the operation-scoped
    lifetime uniqueness makes an exact replay resolve the very same
    session, one session per frozen operation ever. After the caller's
    provider adapter minted the staging upload, ``record_provider_identity``
    is the fenced post-create write that lands the private identity: the
    identical identity replays idempotently, an absent one is stored, and a
    divergent one surfaces as the closed provider-state-invalid rejection
    so the caller can abort its fresh orphan. ``load_owned_session`` is the
    owner-checked resume read; ``record_provider_part`` persists one part
    fact exactly as the provider's ``ListParts`` observed it.
    ``claim_completion`` mints the finite completion lease (or observes the
    frozen committed replay) and ``record_terminal_result`` is the fenced
    terminal write — compare-and-set on the claim token and state — for
    both the frozen committed result and the closed failure obligations.
    ``claim_cleanup_batch`` strikes the 24-hour expiry and leases a bounded
    batch of exact cleanup obligations, and ``record_cleanup_result`` is
    the lease-fenced cleanup outcome write. Every closed failure crosses
    the boundary as the typed
    :class:`~personal_os.multipart_upload.errors.MultipartUploadError`;
    provider calls never occur while a transaction is open.
    """

    async def reserve_session(
        self,
        *,
        operation: SmallFileUploadOperation,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionRecord: ...

    async def record_provider_identity(
        self,
        *,
        session_id: MultipartUploadSessionId,
        staging_key: str,
        provider_upload_id: MultipartProviderUploadId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionRecord: ...

    async def load_owned_session(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionRecord: ...

    async def record_provider_part(
        self,
        *,
        session_id: MultipartUploadSessionId,
        part_number: int,
        etag: MultipartProviderPartETag,
        verified_size_bytes: int,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> None: ...

    async def claim_completion(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionClaim: ...

    async def record_terminal_result(
        self,
        *,
        claim: MultipartSessionClaim,
        result: SmallFileTerminalResult | None = None,
        failure_state: MultipartSessionState | None = None,
        diagnostic_context: DiagnosticContext,
    ) -> None: ...

    async def claim_cleanup_batch(
        self,
        *,
        batch_limit: int,
        diagnostic_context: DiagnosticContext,
    ) -> Sequence[MultipartCleanupClaim]: ...

    async def record_cleanup_result(
        self,
        *,
        claim: MultipartCleanupClaim,
        is_succeeded: bool,
        failure_reason: ErrorCode | None = None,
        diagnostic_context: DiagnosticContext,
    ) -> None: ...
