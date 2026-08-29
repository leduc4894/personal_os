"""Durable multipart session store over the canonical baseline.

:class:`PostgresqlMultipartUploadStore` implements the provider-neutral
:class:`~personal_os.multipart_upload.ports.MultipartSessionStore` port
against the ``20260828_01``, ``20260828_03`` and ``20260828_04``
migrations, and
:class:`PostgresqlMultipartSessionEvidenceStore` implements the frozen
bound-operation evidence read over the session row's AEAD-sealed raw
operation-token preimage (migration ``20260828_04``): the reservation
seals the token inside its own transaction and refreshes the seal on a
replayed reservation's rotated token, and the evidence read opens the
seal, proves it against the operation row's stored one-way token hash and
claims the still-pending operation row into ``receiving`` behind the
shared operation-identity advisory lock so the small-file publication
fence accepts the session. The store owns
no provider I/O of any kind: no R2 SDK, object-storage client or network
call is ever imported or touched while a transaction is open, so the
orchestration service crosses to the provider strictly between the
committed database steps. ``reserve_session`` runs one ``READ COMMITTED``
transaction behind the shared upload-operation identity advisory lock and
a ``SELECT ... FOR UPDATE`` on the operation's session row: the
reservation happens BEFORE any provider call and carries no provider
identity (spec 6.1 persist-before-create) — that identity-absent row is
the durable recovery state that makes an ambiguous provider create
retryable — and the operation's lifetime uniqueness admits exactly one
session per frozen operation, so a concurrent or sequential replay
resolves that same session and the store mints no provider work twice.
``record_provider_identity`` is the fenced post-create write that lands
the private staging identity exactly once: the identical identity replays
idempotently and a divergent one is the closed provider-state-invalid
rejection, so a caller that minted a second upload can abort its orphan
instead of silently discarding it. ``claim_completion`` serializes the
completion family (``completing``/``verifying``/``promoting``) behind an
explicit finite lease: a fresh claim mints a new opaque token, a caller
that arrives while a live lease stands observes the closed retryable
``multipart_completion_in_progress`` token, and a claimant that returns
after its lease expired is fenced by the compare-and-set token guard of
every terminal write — a replacement claimant's state can never be
mutated by the stale claimant. ``record_terminal_result`` lands either the
frozen committed result (whose exact replay converges idempotently) or
one of the closed failure obligations together with its exact cleanup
obligation. ``record_provider_part`` persists a part fact exactly as the
provider's ``ListParts`` observed it — the caller supplies the fact only
after the provider confirmed it — and admits only facts that match the
session's frozen geometry, treating a conflicting observation of one part
as the closed provider-state-invalid rejection. ``claim_cleanup_batch``
strikes the 24-hour deadline over the forward states (clearing any dead
completion claim) and leases a bounded, skip-locked batch of due cleanup
obligations by rotating the lease token; ``record_cleanup_result`` is the
lease-fenced outcome write whose failure path persists the closed reason
token and an exact bounded next-retry deadline. Every statement is
schema-qualified and parameter-bound; the staging key, provider upload ID
and provider ETag are private provider identity that never renders
outside a redacted ``repr``, never enters a log, metric or error, and
driver failures cross the boundary only through the closed multipart
registry's retryable dependency-unavailable token.
"""

from __future__ import annotations

import asyncio
import random
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4, uuid7

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.multipart_upload.contracts import (
    MultipartPartGeometry,
    MultipartSessionState,
    MultipartUploadSessionId,
    compute_multipart_session_expiry,
)
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.ports import (
    MULTIPART_TERMINAL_FAILURE_STATES,
    MultipartCleanupClaim,
    MultipartOperationTokenCodecPort,
    MultipartProviderPartETag,
    MultipartProviderUploadId,
    MultipartSessionClaim,
    MultipartSessionRecord,
    SealedMultipartOperationToken,
)
from personal_os.small_file_sync.contracts import (
    BoundSmallFileOperation,
    SmallFileDeviceContext,
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
    SmallFileUploadOperation,
    UploadOperationToken,
)
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import (
    RETRY_JITTER_MAXIMUM_SECONDS,
    RETRY_JITTER_MINIMUM_SECONDS,
    DatabaseFailureKind,
    classify_database_failure,
)
from postgresql_source_store.locks import advisory_xact_lock_statement
from postgresql_source_store.small_file_sync_operations import (
    STATE_FAILED,
    STATE_PENDING,
    STATE_RECEIVING,
    UPLOAD_OPERATION_LOCK_NAMESPACE,
    SmallFileOperationRow,
    _bound_operation_from_row,
    operation_fingerprint_matches,
    operation_token_lookup_statement,
    receive_claim_statement,
    upload_operation_identity_lock_key,
    upload_operation_lock_statement,
    upload_operation_token_hash,
)
from postgresql_source_store.tables import (
    multipart_parts,
    multipart_uploads,
    small_file_upload_operations,
)

#: Finite completion lease: one claimant owns the serialized completion of
#: a session for at most ten minutes of wall clock. The lease is minted by
#: ``claim_completion`` and enforced by the claim-token guard of every
#: terminal write; expiry alone fences a slow or crashed claimant because
#: the guard refuses to accept a token whose lease has passed.
MULTIPART_COMPLETION_LEASE_SECONDS: Final[int] = 600

#: Finite cleanup lease: one cleanup worker owns an obligated session row
#: for at most fifteen minutes of wall clock. The claim moves the row's
#: next-retry deadline to the lease deadline, so a live claim hides the row
#: from further sweeps; once the lease passes — the crashed-worker case —
#: the row reappears, the next sweep rotates the lease token and the stale
#: worker's outcome write fails the token guard.
MULTIPART_CLEANUP_LEASE_SECONDS: Final[int] = 900

#: Bounded cleanup backoff: the first retry follows one minute after a
#: failed cleanup and every further attempt doubles the delay up to the
#: one-hour ceiling, so a persistently failing cleanup stays visible with
#: an exact next retry instead of becoming a hot loop.
MULTIPART_CLEANUP_RETRY_BASE_SECONDS: Final[int] = 60
MULTIPART_CLEANUP_RETRY_MAXIMUM_SECONDS: Final[int] = 3600

#: Hard ceiling of one cleanup claim batch: the workflow may ask for less,
#: never more.
MULTIPART_CLEANUP_BATCH_MAXIMUM: Final[int] = 100

#: Entropy bytes behind every minted opaque public session ID.
_SESSION_ID_ENTROPY_BYTES: Final[int] = 32

#: The five forward states the 24-hour deadline can still strike
#: (spec 4.2): every mutation path rechecks the deadline over exactly
#: these states, while the terminal and obligation states survive it.
_FORWARD_SESSION_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CREATED,
        MultipartSessionState.UPLOADING,
        MultipartSessionState.COMPLETING,
        MultipartSessionState.VERIFYING,
        MultipartSessionState.PROMOTING,
    }
)

#: The completion family fenced by the durable claimant and its lease.
_COMPLETION_CLAIMED_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.COMPLETING,
        MultipartSessionState.VERIFYING,
        MultipartSessionState.PROMOTING,
    }
)

#: The states that admit a provider-observed part fact: a fact lands while
#: the session still receives uploads or while a completion claimant is
#: reconciling the provider's part list.
_PART_RECORDING_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CREATED,
        MultipartSessionState.UPLOADING,
        MultipartSessionState.COMPLETING,
    }
)

#: The states that admit the fenced post-create provider identity write:
#: only the two pre-completion states of a session whose completion no
#: claimant holds yet.
_IDENTITY_RECORDING_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CREATED,
        MultipartSessionState.UPLOADING,
    }
)

#: The failure-obligation states whose cleanup claim resolves the readable
#: failure state into the ``cleanup_pending`` obligation.
_CLEANUP_OBLIGATION_STATES: Final[frozenset[MultipartSessionState]] = frozenset(
    {
        MultipartSessionState.CANCELLING,
        MultipartSessionState.EXPIRED,
        MultipartSessionState.INTEGRITY_FAILED,
        MultipartSessionState.POLICY_DENIED,
    }
)

_CLEANUP_UNFINISHED_STATES: Final[tuple[str, ...]] = ("pending", "failed")

#: One row of the session table: a SQLAlchemy row mapping from the
#: adapter's ``.mappings()`` results or an equivalent mapping in tests.
type _MappedRow = RowMapping | Mapping[str, Any]

_MULTIPART_ROW_COLUMNS: Final[tuple[str, ...]] = (
    "multipart_upload_id",
    "session_id",
    "workspace_id",
    "device_id",
    "operation_id",
    "declared_sha256",
    "declared_size_bytes",
    "declared_media_type",
    "base_version_id",
    "policy_revision_number",
    "part_size_bytes",
    "part_count",
    "staging_key",
    "provider_upload_id",
    "operation_token_ciphertext",
    "operation_token_nonce",
    "operation_token_key_id",
    "state",
    "claim_token",
    "claim_expires_at",
    "result_kind",
    "result_source_id",
    "result_source_version_id",
    "result_content_version",
    "result_committed_at",
    "cleanup_state",
    "cleanup_attempt_count",
    "cleanup_next_retry_at",
    "cleanup_reason_code",
    "expires_at",
)


def mint_multipart_session_id() -> MultipartUploadSessionId:
    """Mint one fresh opaque public session ID.

    ``secrets.token_urlsafe(32)`` yields 43 printable base64url characters:
    within the domain grammar's 32-128 bound, never a raw canonical UUID,
    and never derived from any database identifier, staging key or provider
    detail.
    """
    return MultipartUploadSessionId(secrets.token_urlsafe(_SESSION_ID_ENTROPY_BYTES))


def compute_completion_lease_expiry(now: datetime) -> datetime:
    """Compute the finite completion lease deadline from the aware clock."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC) + timedelta(seconds=MULTIPART_COMPLETION_LEASE_SECONDS)


def compute_cleanup_lease_expiry(now: datetime) -> datetime:
    """Compute the finite cleanup lease deadline from the aware clock."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC) + timedelta(seconds=MULTIPART_CLEANUP_LEASE_SECONDS)


def compute_cleanup_next_retry(now: datetime, *, attempt_count: int) -> datetime:
    """Compute the bounded exponential cleanup retry deadline.

    ``attempt_count`` is the ordinal of the attempt that just failed: the
    wait after the first failed attempt is the base delay, every further
    failure doubles it, and the ceiling caps the wait so a persistently
    failing cleanup keeps an exact, bounded next-retry deadline instead of
    an unbounded one.
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if attempt_count < 1:
        raise ValueError("attempt_count must be a positive attempt ordinal")
    delay_seconds = min(
        MULTIPART_CLEANUP_RETRY_BASE_SECONDS * (2 ** (attempt_count - 1)),
        MULTIPART_CLEANUP_RETRY_MAXIMUM_SECONDS,
    )
    return now.astimezone(UTC) + timedelta(seconds=delay_seconds)


def require_terminal_failure_state(failure_state: MultipartSessionState) -> None:
    """Validate a fenced terminal failure obligation against the closed set.

    Only the three claimant-landable failure obligations of spec 4.2 are
    admitted; expiry belongs to the sweep alone, so any other state is the
    closed session-state-invalid rejection.
    """

    if failure_state not in MULTIPART_TERMINAL_FAILURE_STATES:
        raise MultipartUploadError(ErrorCode.MULTIPART_SESSION_STATE_INVALID)


def _multipart_row_select() -> sa.Select[tuple[Any, ...]]:
    """Build the schema-qualified full-column select of the session table."""

    columns = [getattr(multipart_uploads.c, column_name) for column_name in _MULTIPART_ROW_COLUMNS]
    return sa.select(*columns)


def multipart_session_select_statement(
    session_id: MultipartUploadSessionId, *, for_update: bool = True
) -> sa.Select[tuple[Any, ...]]:
    """Build the session lookup by its opaque public ID, row-locked by default.

    ``for_update`` (the default) takes the row lock every session mutation
    runs behind; the lock-free shape serves the owner-checked reads and the
    query-plan probes.
    """
    statement = _multipart_row_select().where(multipart_uploads.c.session_id == session_id.value)
    return statement.with_for_update() if for_update else statement


def multipart_operation_select_statement(
    operation_id: UUID, *, for_update: bool = True
) -> sa.Select[tuple[Any, ...]]:
    """Build the session lookup by its frozen operation, row-locked by default.

    The operation-scoped lifetime uniqueness makes this the reservation's
    replay point: the row it finds is the one session the operation may
    ever own.
    """
    statement = _multipart_row_select().where(multipart_uploads.c.operation_id == operation_id)
    return statement.with_for_update() if for_update else statement


def multipart_session_insert_statement(
    *,
    multipart_upload_id: UUID,
    session_id_value: str,
    workspace_id: UUID,
    device_id: UUID,
    operation_id: UUID,
    declared_sha256: str,
    declared_size_bytes: int,
    declared_media_type: str,
    base_version_id: UUID | None,
    policy_revision_number: int,
    part_size_bytes: int,
    part_count: int,
    expires_at: datetime,
    sealed_token: SealedMultipartOperationToken | None = None,
) -> sa.Insert:
    """Build the parameter-bound reservation insert of one created session.

    The row opens in the ``created`` state with no claim, no cleanup
    obligation and no provider identity (spec 6.1 persist-before-create):
    the private staging identity arrives only through the fenced
    post-create write, so this insert already is the durable recovery
    state that makes an ambiguous provider create retryable. The optional
    sealed token is the AEAD-sealed raw preimage of the frozen operation's
    token hash — sealed material only, never plaintext, bound as
    parameters together with the identity of the keyring key that sealed
    it.
    """
    return sa.insert(multipart_uploads).values(
        multipart_upload_id=multipart_upload_id,
        session_id=session_id_value,
        workspace_id=workspace_id,
        device_id=device_id,
        operation_id=operation_id,
        declared_sha256=declared_sha256,
        declared_size_bytes=declared_size_bytes,
        declared_media_type=declared_media_type,
        base_version_id=base_version_id,
        policy_revision_number=policy_revision_number,
        part_size_bytes=part_size_bytes,
        part_count=part_count,
        staging_key=None,
        provider_upload_id=None,
        operation_token_ciphertext=None if sealed_token is None else sealed_token.ciphertext,
        operation_token_nonce=None if sealed_token is None else sealed_token.nonce,
        operation_token_key_id=None if sealed_token is None else sealed_token.key_id,
        state=MultipartSessionState.CREATED.value,
        claim_token=None,
        claim_expires_at=None,
        cleanup_state="none",
        cleanup_attempt_count=0,
        cleanup_next_retry_at=None,
        cleanup_reason_code=None,
        expires_at=expires_at,
    )


def operation_token_seal_update_statement(
    *,
    session_id_value: str,
    sealed_token: SealedMultipartOperationToken,
) -> sa.Update:
    """Build the parameter-bound refresh of one session's sealed token.

    A replayed reservation may arrive with a rotated raw token (the
    small-file operation store rotates tokens on re-reserve); refreshing
    the seal in the same reservation transaction keeps the session row's
    sealed preimage naming the operation row's current token hash.
    """

    return (
        sa.update(multipart_uploads)
        .values(
            operation_token_ciphertext=sealed_token.ciphertext,
            operation_token_nonce=sealed_token.nonce,
            operation_token_key_id=sealed_token.key_id,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(multipart_uploads.c.session_id == session_id_value)
    )


def operation_row_by_id_select_statement(
    operation_id: UUID, *, for_update: bool = False
) -> sa.Select[tuple[Any, ...]]:
    """Build the operation-row lookup by its frozen primary identity.

    The multipart evidence read resolves its session's frozen operation
    row by the session row's ``operation_id`` reference — not by any token
    preimage — so the sealed token is verified against the row's stored
    hash instead of driving the lookup. The read stays lock-free by
    default: the evidence path takes the shared operation-identity
    advisory lock before any claim, mirroring the small-file receive
    boundary.
    """

    statement = sa.select(
        small_file_upload_operations.c.operation_id,
        small_file_upload_operations.c.operation_token_hash,
        small_file_upload_operations.c.workspace_id,
        small_file_upload_operations.c.device_id,
        small_file_upload_operations.c.event_id,
        small_file_upload_operations.c.idempotency_key,
        small_file_upload_operations.c.operation_kind,
        small_file_upload_operations.c.declared_sha256,
        small_file_upload_operations.c.declared_size_bytes,
        small_file_upload_operations.c.declared_media_type,
        small_file_upload_operations.c.policy_revision_number,
        small_file_upload_operations.c.reserved_source_id,
        small_file_upload_operations.c.update_source_id,
        small_file_upload_operations.c.update_base_version_id,
        small_file_upload_operations.c.normalized_locator,
        small_file_upload_operations.c.locator_fingerprint,
        small_file_upload_operations.c.state,
        small_file_upload_operations.c.safe_error_code,
        small_file_upload_operations.c.result_kind,
        small_file_upload_operations.c.result_source_id,
        small_file_upload_operations.c.result_source_version_id,
        small_file_upload_operations.c.result_content_version,
        small_file_upload_operations.c.result_committed_at,
        small_file_upload_operations.c.expires_at,
    ).where(small_file_upload_operations.c.operation_id == operation_id)
    return statement.with_for_update() if for_update else statement


def provider_identity_update_statement(
    *,
    session_id_value: str,
    staging_key: str,
    provider_upload_id_value: str,
) -> sa.Update:
    """Build the compare-and-set post-create provider identity write.

    The guard admits exactly one pre-completion state row that carries no
    identity and no claim lease, so the identity lands once: a concurrent
    or replayed winner is visible as a zero-row update the caller resolves
    through its own identity comparison. The private values cross only as
    bound parameters, never as SQL text.
    """
    return (
        sa.update(multipart_uploads)
        .values(
            staging_key=staging_key,
            provider_upload_id=provider_upload_id_value,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            multipart_uploads.c.session_id == session_id_value,
            multipart_uploads.c.state.in_(
                (MultipartSessionState.CREATED.value, MultipartSessionState.UPLOADING.value)
            ),
            multipart_uploads.c.staging_key.is_(None),
            multipart_uploads.c.provider_upload_id.is_(None),
            multipart_uploads.c.claim_token.is_(None),
        )
    )


def completion_claim_transition_statement(
    *,
    session_id_value: str,
    claim_token: UUID,
    claim_expires_at: datetime,
) -> sa.Update:
    """Build the guarded claim of the serialized completion.

    The guard admits exactly the two pre-completion states, so the claim
    and its lease land together with the ``completing`` transition and a
    concurrent winner is visible as a zero-row update.
    """
    return (
        sa.update(multipart_uploads)
        .values(
            state=MultipartSessionState.COMPLETING.value,
            claim_token=claim_token,
            claim_expires_at=claim_expires_at,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            multipart_uploads.c.session_id == session_id_value,
            multipart_uploads.c.state.in_(
                (MultipartSessionState.CREATED.value, MultipartSessionState.UPLOADING.value)
            ),
        )
    )


def completion_lease_replacement_statement(
    *,
    session_id_value: str,
    claim_token: UUID,
    claim_expires_at: datetime,
    now: datetime,
) -> sa.Update:
    """Build the guarded lease rotation over an expired completion claim.

    The state stays inside the completion family; only the fencing token
    and its finite deadline rotate, and the guard refuses any row whose
    lease is still live, so a replacement claimant can never overwrite an
    active one.
    """
    return (
        sa.update(multipart_uploads)
        .values(
            claim_token=claim_token,
            claim_expires_at=claim_expires_at,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            multipart_uploads.c.session_id == session_id_value,
            multipart_uploads.c.state.in_(
                tuple(state.value for state in _COMPLETION_CLAIMED_STATES)
            ),
            multipart_uploads.c.claim_expires_at <= now,
        )
    )


def terminal_result_update_statement(
    *,
    session_id_value: str,
    claim_token: UUID,
    result: SmallFileTerminalResult,
) -> sa.Update:
    """Build the compare-and-set frozen-result terminal write.

    The guard requires the row's live claim token and a state inside the
    completion family, so a stale claimant — a token the row no longer
    carries, or one whose lease already passed — can never land a result.
    The write releases the claim lease together with the frozen result:
    a committed session owns no completion claim.
    """
    return (
        sa.update(multipart_uploads)
        .values(
            state=MultipartSessionState.COMMITTED.value,
            claim_token=None,
            claim_expires_at=None,
            result_kind=result.result_kind.value,
            result_source_id=result.source_id,
            result_source_version_id=result.source_version_id,
            result_content_version=result.content_version,
            result_committed_at=result.committed_at,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            multipart_uploads.c.session_id == session_id_value,
            multipart_uploads.c.claim_token == claim_token,
            multipart_uploads.c.state.in_(
                tuple(state.value for state in _COMPLETION_CLAIMED_STATES)
            ),
        )
    )


def terminal_failure_update_statement(
    *,
    session_id_value: str,
    claim_token: UUID,
    failure_state: MultipartSessionState,
    now: datetime,
) -> sa.Update:
    """Build the compare-and-set failure-obligation terminal write.

    The closed failure state, its exact cleanup obligation — one scheduled
    attempt, due now — and the released claim lease land together, so the
    obligation is durable the moment the claimant decides the failure.
    """
    return (
        sa.update(multipart_uploads)
        .values(
            state=failure_state.value,
            claim_token=None,
            claim_expires_at=None,
            cleanup_state="pending",
            cleanup_attempt_count=1,
            cleanup_next_retry_at=now,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            multipart_uploads.c.session_id == session_id_value,
            multipart_uploads.c.claim_token == claim_token,
            multipart_uploads.c.state.in_(
                tuple(state.value for state in _COMPLETION_CLAIMED_STATES)
            ),
        )
    )


def multipart_part_insert_statement(
    *,
    multipart_part_id: UUID,
    multipart_upload_id: UUID,
    part_number: int,
    offset_bytes: int,
    size_bytes: int,
    provider_etag_value: str,
    completed_at: datetime,
) -> sa.Insert:
    """Build the parameter-bound insert of one provider-confirmed part fact."""

    return sa.insert(multipart_parts).values(
        multipart_part_id=multipart_part_id,
        multipart_upload_id=multipart_upload_id,
        part_number=part_number,
        offset_bytes=offset_bytes,
        size_bytes=size_bytes,
        provider_etag=provider_etag_value,
        verified_size_bytes=size_bytes,
        completed_at=completed_at,
    )


def uploading_transition_statement(*, session_id_value: str) -> sa.Update:
    """Build the guarded ``created -> uploading`` advance on first part fact."""

    return (
        sa.update(multipart_uploads)
        .values(
            state=MultipartSessionState.UPLOADING.value,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            multipart_uploads.c.session_id == session_id_value,
            multipart_uploads.c.state == MultipartSessionState.CREATED.value,
        )
    )


def expiry_sweep_select_statement(*, now: datetime, batch_limit: int) -> sa.Select[tuple[Any, ...]]:
    """Build the bounded skip-locked sweep over the strikable forward states.

    The predicate matches the shipped partial expiry index exactly: only
    the five forward states whose deadline has passed are addressed, in
    deadline order, at most ``batch_limit`` rows at a time.
    """
    return (
        _multipart_row_select()
        .where(
            multipart_uploads.c.state.in_(tuple(state.value for state in _FORWARD_SESSION_STATES)),
            multipart_uploads.c.expires_at <= now,
        )
        .order_by(multipart_uploads.c.expires_at)
        .limit(batch_limit)
        .with_for_update(skip_locked=True)
    )


def expiry_strike_update_statement(*, session_id_value: str, now: datetime) -> sa.Update:
    """Build the guarded 24-hour strike of one forward session.

    The strike lands the closed expired state with its exact cleanup
    obligation — one scheduled attempt, due now — and releases any dead
    completion claim, so a session the deadline ended can never resume
    uploads, completion or publication.
    """
    return (
        sa.update(multipart_uploads)
        .values(
            state=MultipartSessionState.EXPIRED.value,
            claim_token=None,
            claim_expires_at=None,
            cleanup_state="pending",
            cleanup_attempt_count=1,
            cleanup_next_retry_at=now,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            multipart_uploads.c.session_id == session_id_value,
            multipart_uploads.c.state.in_(tuple(state.value for state in _FORWARD_SESSION_STATES)),
        )
    )


def cleanup_claim_select_statement(
    *, now: datetime, batch_limit: int
) -> sa.Select[tuple[Any, ...]]:
    """Build the bounded skip-locked claim over the due cleanup obligations.

    The predicate matches the shipped partial cleanup-claim index exactly:
    only obligations whose next retry is due are addressed, in retry order,
    at most ``batch_limit`` rows at a time.
    """
    return (
        _multipart_row_select()
        .where(
            multipart_uploads.c.cleanup_state.in_(_CLEANUP_UNFINISHED_STATES),
            multipart_uploads.c.cleanup_next_retry_at <= now,
        )
        .order_by(multipart_uploads.c.cleanup_next_retry_at)
        .limit(batch_limit)
        .with_for_update(skip_locked=True)
    )


def cleanup_claim_update_statement(
    *,
    session_id_value: str,
    claim_token: UUID,
    claim_expires_at: datetime,
    now: datetime,
) -> sa.Update:
    """Build the guarded cleanup lease rotation over one due obligation.

    The lease token and its finite deadline rotate; a readable failure
    state resolves into the ``cleanup_pending`` obligation in the same
    write (the closed machine's failure exit). The row's next-retry
    deadline moves to the lease deadline, so a live claim hides the row
    from every further sweep until its lease passes — and a crashed
    worker's row reappears for the next sweep exactly then.
    """
    return (
        sa.update(multipart_uploads)
        .values(
            state=sa.case(
                (
                    multipart_uploads.c.state.in_(
                        tuple(state.value for state in _CLEANUP_OBLIGATION_STATES)
                    ),
                    MultipartSessionState.CLEANUP_PENDING.value,
                ),
                else_=multipart_uploads.c.state,
            ),
            claim_token=claim_token,
            claim_expires_at=claim_expires_at,
            cleanup_next_retry_at=claim_expires_at,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            multipart_uploads.c.session_id == session_id_value,
            multipart_uploads.c.cleanup_state.in_(_CLEANUP_UNFINISHED_STATES),
            multipart_uploads.c.cleanup_next_retry_at <= now,
        )
    )


def cleanup_success_update_statement(
    *,
    session_id_value: str,
    claim_token: UUID,
    now: datetime,
) -> sa.Update:
    """Build the lease-fenced successful cleanup outcome write.

    The terminal ``cleaned`` state, the succeeded obligation and the
    released lease land together behind the live claim-token guard, so a
    stale worker can never finish another worker's obligation.
    """
    return (
        sa.update(multipart_uploads)
        .values(
            state=MultipartSessionState.CLEANED.value,
            claim_token=None,
            claim_expires_at=None,
            cleanup_state="succeeded",
            cleanup_next_retry_at=None,
            cleanup_reason_code=None,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            multipart_uploads.c.session_id == session_id_value,
            multipart_uploads.c.claim_token == claim_token,
            multipart_uploads.c.cleanup_state.in_(_CLEANUP_UNFINISHED_STATES),
            multipart_uploads.c.claim_expires_at > now,
        )
    )


def cleanup_failure_update_statement(
    *,
    session_id_value: str,
    claim_token: UUID,
    failure_reason: ErrorCode,
    attempt_count: int,
    next_retry_at: datetime,
    now: datetime,
) -> sa.Update:
    """Build the lease-fenced failed cleanup outcome write.

    The closed reason token, the count of the newly scheduled attempt and
    the exact bounded next-retry deadline land together behind the live
    claim-token guard, so a stale worker can never schedule another
    worker's retry.
    """
    return (
        sa.update(multipart_uploads)
        .values(
            claim_token=None,
            claim_expires_at=None,
            cleanup_state="failed",
            cleanup_reason_code=failure_reason.value,
            cleanup_attempt_count=attempt_count,
            cleanup_next_retry_at=next_retry_at,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            multipart_uploads.c.session_id == session_id_value,
            multipart_uploads.c.claim_token == claim_token,
            multipart_uploads.c.cleanup_state.in_(_CLEANUP_UNFINISHED_STATES),
            multipart_uploads.c.claim_expires_at > now,
        )
    )


def map_multipart_database_failure(cause: BaseException) -> MultipartUploadError:
    """Map a database or driver failure onto the closed error boundary.

    The multipart registry carries the retryable
    ``multipart_dependency_unavailable`` token, so every database failure —
    contention exhausted after the bounded retries, unavailability, an
    integrity-constraint violation or any unclassified SQLSTATE — crosses
    the boundary as that typed token, and a non-database exception is an
    internal bug of the same class. The cause remains chained only; its
    SQLSTATE, constraint name and text never enter the error.
    """
    del cause
    return MultipartUploadError(ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class MultipartDatabaseRetryPolicy:
    """Bounded retry for the multipart store over the shared SQLSTATE classifier.

    At most ``maximum_attempts`` attempts run with the shared cancellable
    50-250 ms jitter. Typed application errors pass through untouched;
    every other failure — contention exhausted, unavailability, an
    integrity-constraint violation or an unclassified database failure —
    maps through :func:`map_multipart_database_failure`, whose closed token
    never leaks SQLSTATE, SQL text, parameters or driver messages.
    """

    maximum_attempts: int = 3

    async def run[T](
        self,
        operation: Callable[[int], Awaitable[T]],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> T:
        for attempt in range(1, self.maximum_attempts + 1):
            try:
                return await operation(attempt)
            except MultipartUploadError:
                raise
            except Exception as cause:
                failure_kind = classify_database_failure(cause)
                if failure_kind is DatabaseFailureKind.NOT_DATABASE:
                    raise map_multipart_database_failure(cause) from cause
                if failure_kind is DatabaseFailureKind.CONTENTION:
                    if attempt == self.maximum_attempts:
                        raise map_multipart_database_failure(cause) from cause
                else:
                    raise map_multipart_database_failure(cause) from cause
                await sleep(jitter(RETRY_JITTER_MINIMUM_SECONDS, RETRY_JITTER_MAXIMUM_SECONDS))
        raise AssertionError("retry loop exhausted without a result")


@dataclass(frozen=True, slots=True)
class MultipartSessionRow:
    """The hydrated view of one session row used by the transitions.

    The private staging identity rides on this view only between the
    database and the port's redacted record; the dataclass renders nothing
    outside its redacted ``repr``.
    """

    multipart_upload_id: UUID
    session_id_value: str
    workspace_id: UUID
    device_id: UUID
    operation_id: UUID
    declared_sha256: str
    declared_size_bytes: int
    declared_media_type: str
    base_version_id: UUID | None
    policy_revision_number: int
    part_size_bytes: int
    part_count: int
    staging_key: str | None
    provider_upload_id_value: str | None
    sealed_ciphertext: str | None
    sealed_nonce: str | None
    sealed_key_id: str | None
    state: MultipartSessionState
    claim_token: UUID | None
    claim_expires_at: datetime | None
    result_kind: str | None
    result_source_id: UUID | None
    result_source_version_id: UUID | None
    result_content_version: int | None
    result_committed_at: datetime | None
    cleanup_state: str
    cleanup_attempt_count: int
    cleanup_next_retry_at: datetime | None
    cleanup_reason_code: str | None
    expires_at: datetime

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    @classmethod
    def from_row_mapping(cls, row: _MappedRow) -> MultipartSessionRow:
        """Build the typed row from one named result row of the lookup."""

        try:
            state = MultipartSessionState(row["state"])
        except ValueError:
            raise MultipartUploadError(ErrorCode.MULTIPART_SESSION_STATE_INVALID) from None
        return cls(
            multipart_upload_id=row["multipart_upload_id"],
            session_id_value=row["session_id"],
            workspace_id=row["workspace_id"],
            device_id=row["device_id"],
            operation_id=row["operation_id"],
            declared_sha256=row["declared_sha256"],
            declared_size_bytes=int(row["declared_size_bytes"]),
            declared_media_type=row["declared_media_type"],
            base_version_id=row["base_version_id"],
            policy_revision_number=int(row["policy_revision_number"]),
            part_size_bytes=int(row["part_size_bytes"]),
            part_count=int(row["part_count"]),
            staging_key=row["staging_key"],
            provider_upload_id_value=row["provider_upload_id"],
            sealed_ciphertext=row["operation_token_ciphertext"],
            sealed_nonce=row["operation_token_nonce"],
            sealed_key_id=row["operation_token_key_id"],
            state=state,
            claim_token=row["claim_token"],
            claim_expires_at=row["claim_expires_at"],
            result_kind=row["result_kind"],
            result_source_id=row["result_source_id"],
            result_source_version_id=row["result_source_version_id"],
            result_content_version=(
                None
                if row["result_content_version"] is None
                else int(row["result_content_version"])
            ),
            result_committed_at=row["result_committed_at"],
            cleanup_state=row["cleanup_state"],
            cleanup_attempt_count=int(row["cleanup_attempt_count"]),
            cleanup_next_retry_at=row["cleanup_next_retry_at"],
            cleanup_reason_code=row["cleanup_reason_code"],
            expires_at=row["expires_at"],
        )

    def terminal_result(self) -> SmallFileTerminalResult | None:
        """Hydrate the frozen terminal result of a committed row, if any.

        A committed row missing any result field is an integrity violation
        of the migration's terminal-shape CHECK and fails closed as the
        closed session-state-invalid error, never as raw database
        evidence.
        """
        if self.state is not MultipartSessionState.COMMITTED:
            return None
        if (
            self.result_kind is None
            or self.result_source_id is None
            or self.result_source_version_id is None
            or self.result_content_version is None
            or self.result_committed_at is None
        ):
            raise MultipartUploadError(ErrorCode.MULTIPART_SESSION_STATE_INVALID)
        try:
            result_kind = SmallFileTerminalResultKind(self.result_kind)
        except ValueError:
            raise MultipartUploadError(ErrorCode.MULTIPART_SESSION_STATE_INVALID) from None
        return SmallFileTerminalResult(
            result_kind=result_kind,
            source_id=self.result_source_id,
            source_version_id=self.result_source_version_id,
            content_version=self.result_content_version,
            committed_at=self.result_committed_at,
        )

    def is_forward_expired(self, now: datetime) -> bool:
        """Report whether the 24-hour deadline already struck a forward state."""

        return self.state in _FORWARD_SESSION_STATES and self.expires_at <= now

    def has_provider_identity(self) -> bool:
        """Report whether the fenced post-create identity write already landed."""

        return self.staging_key is not None and self.provider_upload_id_value is not None

    def carries_provider_identity(self, staging_key: str, provider_upload_id_value: str) -> bool:
        """Report whether the row already carries exactly this identity."""

        return (
            self.staging_key == staging_key
            and self.provider_upload_id_value == provider_upload_id_value
        )

    def geometry(self) -> MultipartPartGeometry:
        """Rebuild the session's frozen part geometry from its row."""

        return MultipartPartGeometry(
            total_size_bytes=self.declared_size_bytes,
            part_size_bytes=self.part_size_bytes,
            part_count=self.part_count,
        )


def _session_not_found() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_SESSION_NOT_FOUND)


def _state_invalid() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_SESSION_STATE_INVALID)


def multipart_sealed_token_material(
    row: MultipartSessionRow,
) -> SealedMultipartOperationToken:
    """Extract one session row's sealed token material, or fail closed.

    The three sealed columns are present or absent together (the migration's
    biconditional CHECK): an absent or partial seal is unrecoverable
    evidence surfaced as the closed session-state-invalid error — never a
    guess and never decrypted material for an owner the caller has not
    proved.
    """

    ciphertext = row.sealed_ciphertext
    nonce = row.sealed_nonce
    key_id = row.sealed_key_id
    if ciphertext is None or nonce is None or key_id is None:
        raise _state_invalid()
    return SealedMultipartOperationToken(key_id=key_id, nonce=nonce, ciphertext=ciphertext)


def _session_expired() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_SESSION_EXPIRED)


def _completion_in_progress() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_COMPLETION_IN_PROGRESS)


def _provider_state_invalid() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID)


def _part_invalid() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_PART_INVALID)


def _session_row_matches_reservation(
    row: MultipartSessionRow,
    operation: SmallFileUploadOperation,
    device_context: SmallFileDeviceContext,
    geometry: MultipartPartGeometry,
) -> bool:
    """Compare a stored session row against one reservation exactly.

    The comparison covers the credential-derived identity and the complete
    declared fingerprint including the derived geometry, so a session row
    that drifted from the frozen operation it claims to serve fails
    closed.
    """
    preflight = operation.preflight
    return (
        row.workspace_id == device_context.workspace_id
        and row.device_id == device_context.device_id
        and row.declared_sha256 == preflight.sha256.hexadecimal
        and row.declared_size_bytes == preflight.size_bytes
        and row.declared_media_type == preflight.media_type.value
        and row.base_version_id == preflight.base_version_id
        and int(row.policy_revision_number) == preflight.policy_revision_number
        and row.part_size_bytes == geometry.part_size_bytes
        and row.part_count == geometry.part_count
    )


class PostgresqlMultipartUploadStore:
    """Durable multipart session store over the canonical PostgreSQL baseline.

    Takes the composition-owned :class:`AsyncEngine`, the injectable aware
    UTC clock that owns every lease, expiry and retry deadline, and the
    injectable generators behind the opaque public session ID and the
    fencing tokens. The row lock order is fixed: the shared upload-
    operation identity advisory lock, then the ``SELECT ... FOR UPDATE``
    session row, then the guarded compare-and-set updates. No method of
    this class performs or awaits any provider call, and none may ever be
    handed an open transaction by a caller that is about to cross to the
    provider.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        clock: Callable[[], datetime],
        retry: MultipartDatabaseRetryPolicy | None = None,
        session_id_generator: Callable[[], MultipartUploadSessionId] = mint_multipart_session_id,
        claim_token_generator: Callable[[], UUID] = uuid4,
        identity_generator: Callable[[], UUID] = uuid7,
        token_codec: MultipartOperationTokenCodecPort | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._retry = retry if retry is not None else MultipartDatabaseRetryPolicy()
        self._session_id_generator = session_id_generator
        self._claim_token_generator = claim_token_generator
        self._identity_generator = identity_generator
        self._token_codec = token_codec

    async def reserve_session(
        self,
        *,
        operation: SmallFileUploadOperation,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionRecord:
        """Land or exactly replay the one session a frozen operation owns.

        The reservation happens BEFORE any provider call (spec 6.1) and
        carries no provider identity: the inserted ``created`` row is the
        durable recovery state that makes an ambiguous provider create
        retryable. Behind the operation-identity advisory lock and the
        row-locked operation-scoped lookup, a fresh reservation inserts
        the session with its frozen geometry and 24-hour deadline, while an
        existing row is returned unchanged: the operation's lifetime
        uniqueness means the replay resolves the same session without
        minting any second provider workload.
        """
        del diagnostic_context
        try:
            geometry = MultipartPartGeometry.from_size_bytes(operation.preflight.size_bytes)
        except ValueError:
            raise _part_invalid() from None
        return await self._retry.run(
            lambda _attempt: self._reserve_session_once(
                operation=operation,
                device_context=device_context,
                geometry=geometry,
            )
        )

    async def record_provider_identity(
        self,
        *,
        session_id: MultipartUploadSessionId,
        staging_key: str,
        provider_upload_id: MultipartProviderUploadId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionRecord:
        """Land the fenced post-create provider identity of one session.

        The caller invokes this after its provider adapter minted the
        staging upload — the store performs no provider I/O. Behind the
        session row lock: a row carrying no identity stores it once
        (compare-and-set on the identity-absent, claim-free pre-completion
        shape); a row already carrying the identical identity is an
        idempotent replay; a row carrying a divergent identity is the
        closed provider-state-invalid rejection, so the caller can abort
        its fresh orphan upload instead of silently discarding it. A
        session whose completion a claimant already holds, whose identity
        is absent but whose state left the pre-completion states, or whose
        deadline passed fails closed as state-invalid or expired.
        """
        del diagnostic_context
        return await self._retry.run(
            lambda _attempt: self._record_provider_identity_once(
                session_id=session_id,
                staging_key=staging_key,
                provider_upload_id=provider_upload_id,
                device_context=device_context,
            )
        )

    async def load_owned_session(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionRecord:
        """Return the owner-checked hydrated view of one session.

        A foreign workspace or device observes the same closed
        not-found token as an absent session — existence is never leaked
        across owners. A forward state whose 24-hour deadline passed fails
        closed with the expired token; terminal evidence (the committed
        frozen result) survives the deadline and returns unchanged.
        """
        del diagnostic_context
        return await self._retry.run(
            lambda _attempt: self._load_owned_session_once(session_id, device_context)
        )

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
        """Persist one provider-confirmed part fact behind the session lock.

        The caller records a fact only after the provider's ``ListParts``
        confirmed it; this store re-checks ownership, state, expiry and the
        exact frozen geometry before the insert. Re-observing the identical
        fact is an idempotent no-op, a conflicting observation of the same
        part is the closed provider-state-invalid rejection, and the first
        recorded fact advances a ``created`` session to ``uploading``.
        """
        del diagnostic_context
        await self._retry.run(
            lambda _attempt: self._record_provider_part_once(
                session_id=session_id,
                part_number=part_number,
                etag=etag,
                verified_size_bytes=verified_size_bytes,
                device_context=device_context,
            )
        )

    async def claim_completion(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> MultipartSessionClaim:
        """Claim the serialized completion of one session.

        A committed session returns the frozen-replay claim shape whose
        record carries the terminal result. A forward session mints a
        fresh finite lease together with the ``completing`` transition. A
        session already inside the completion family with a live lease
        raises the closed retryable in-progress token; one whose lease
        expired has a new token rotated in, fencing the stale claimant out
        of every terminal write. Failure-obligation and cleaned states
        fail closed as state-invalid.
        """
        del diagnostic_context
        return await self._retry.run(
            lambda _attempt: self._claim_completion_once(session_id, device_context)
        )

    async def record_terminal_result(
        self,
        *,
        claim: MultipartSessionClaim,
        result: SmallFileTerminalResult | None = None,
        failure_state: MultipartSessionState | None = None,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        """Land the fenced terminal write of one completion claim.

        Exactly one outcome is supplied: a frozen result (the committed
        terminal shape, whose identical replay converges idempotently) or
        one of the closed failure obligations (landed together with its
        exact cleanup obligation and released lease). Every path runs the
        compare-and-set claim-token guard, so a claimant whose lease
        expired or was replaced can never mutate the replacement's state.
        """
        del diagnostic_context
        if (result is None) == (failure_state is None):
            raise ValueError("exactly one of result or failure_state is required")
        if failure_state is not None:
            require_terminal_failure_state(failure_state)
        await self._retry.run(
            lambda _attempt: self._record_terminal_result_once(
                claim=claim, result=result, failure_state=failure_state
            )
        )

    async def claim_cleanup_batch(
        self,
        *,
        batch_limit: int,
        diagnostic_context: DiagnosticContext,
    ) -> Sequence[MultipartCleanupClaim]:
        """Strike the deadline and lease one bounded batch of exact cleanups.

        One transaction first strikes the forward sessions whose 24-hour
        deadline passed — releasing any dead completion claim and opening
        the exact cleanup obligation — then leases at most ``batch_limit``
        due obligations by rotating their lease tokens. The skip-locked
        row locks keep concurrent workers on disjoint rows; each returned
        claim carries only its session's exact private resource
        identities and the lease that fences the outcome write.
        """
        del diagnostic_context
        if not 1 <= batch_limit <= MULTIPART_CLEANUP_BATCH_MAXIMUM:
            raise ValueError(f"batch_limit must be 1 to {MULTIPART_CLEANUP_BATCH_MAXIMUM} sessions")
        return await self._retry.run(
            lambda _attempt: self._claim_cleanup_batch_once(batch_limit=batch_limit)
        )

    async def record_cleanup_result(
        self,
        *,
        claim: MultipartCleanupClaim,
        is_succeeded: bool,
        failure_reason: ErrorCode | None = None,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        """Land the lease-fenced cleanup outcome of one exact obligation.

        Success freezes the terminal ``cleaned`` shape with the succeeded
        obligation and released lease. Failure persists the closed reason
        token, the incremented attempt count and the exact bounded
        next-retry deadline, keeping the row visible to the next sweep. A
        stale worker — a token the row no longer carries, or a lease that
        already passed — fails closed as state-invalid.
        """
        del diagnostic_context
        if is_succeeded and failure_reason is not None:
            raise ValueError("failure_reason is admitted only for a failed cleanup")
        if not is_succeeded and failure_reason is None:
            raise ValueError("a failed cleanup requires its closed failure_reason token")
        await self._retry.run(
            lambda _attempt: self._record_cleanup_result_once(
                claim=claim, is_succeeded=is_succeeded, failure_reason=failure_reason
            )
        )

    async def _reserve_session_once(
        self,
        *,
        operation: SmallFileUploadOperation,
        device_context: SmallFileDeviceContext,
        geometry: MultipartPartGeometry,
    ) -> MultipartSessionRecord:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            await connection.execute(
                upload_operation_lock_statement(device_context, operation.preflight)
            )
            operation_row = await self._fetch_operation_row(connection, operation.operation_token)
            if operation_row is None:
                raise _session_not_found()
            if not operation_fingerprint_matches(
                asdict(operation_row), operation.preflight, device_context
            ):
                raise _state_invalid()
            existing = await self._fetch_session_row_by_operation(
                connection, operation_row.operation_id
            )
            sealed_token = (
                None
                if self._token_codec is None
                else self._token_codec.seal_token(token=operation.operation_token)
            )
            if existing is not None:
                if not _session_row_matches_reservation(
                    existing, operation, device_context, geometry
                ):
                    raise _state_invalid()
                if existing.is_forward_expired(self._clock()):
                    raise _session_expired()
                if sealed_token is not None:
                    # A replayed reservation may arrive with a rotated raw
                    # token; refreshing the seal keeps the session row's
                    # sealed preimage naming the operation row's current
                    # token hash.
                    await connection.execute(
                        operation_token_seal_update_statement(
                            session_id_value=existing.session_id_value,
                            sealed_token=sealed_token,
                        )
                    )
                return await self._hydrate_record(connection, existing)
            session_id = self._session_id_generator()
            expires_at = self._compute_session_expiry()
            await connection.execute(
                multipart_session_insert_statement(
                    multipart_upload_id=self._identity_generator(),
                    session_id_value=session_id.value,
                    workspace_id=device_context.workspace_id,
                    device_id=device_context.device_id,
                    operation_id=operation_row.operation_id,
                    declared_sha256=operation.preflight.sha256.hexadecimal,
                    declared_size_bytes=operation.preflight.size_bytes,
                    declared_media_type=operation.preflight.media_type.value,
                    base_version_id=operation.preflight.base_version_id,
                    policy_revision_number=operation.preflight.policy_revision_number,
                    part_size_bytes=geometry.part_size_bytes,
                    part_count=geometry.part_count,
                    expires_at=expires_at,
                    sealed_token=sealed_token,
                )
            )
            reserved = await self._fetch_session_row(connection, session_id)
            if reserved is None:  # pragma: no cover - the insert just committed locally
                raise _state_invalid()
            return await self._hydrate_record(connection, reserved)

    async def _record_provider_identity_once(
        self,
        *,
        session_id: MultipartUploadSessionId,
        staging_key: str,
        provider_upload_id: MultipartProviderUploadId,
        device_context: SmallFileDeviceContext,
    ) -> MultipartSessionRecord:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            row = await self._fetch_session_row(connection, session_id)
            if row is None:
                raise _session_not_found()
            self._require_owner(row, device_context)
            if row.carries_provider_identity(staging_key, provider_upload_id.value):
                return await self._hydrate_record(connection, row)
            if row.state not in _IDENTITY_RECORDING_STATES:
                raise _state_invalid()
            if row.is_forward_expired(self._clock()):
                raise _session_expired()
            if row.has_provider_identity():
                raise _provider_state_invalid()
            guarded = await connection.execute(
                provider_identity_update_statement(
                    session_id_value=row.session_id_value,
                    staging_key=staging_key,
                    provider_upload_id_value=provider_upload_id.value,
                )
            )
            if guarded.rowcount != 1:
                raise _provider_state_invalid()
            refreshed = await self._fetch_session_row(connection, session_id)
            if refreshed is None:  # pragma: no cover - the row is locked
                raise _state_invalid()
            return await self._hydrate_record(connection, refreshed)

    async def _load_owned_session_once(
        self,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
    ) -> MultipartSessionRecord:
        # Lock-free indexed lookup: the session ID's unique constraint alone
        # decides presence, and mutation paths take their own row locks.
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            row = await self._fetch_session_row(connection, session_id, for_update=False)
            if row is None:
                raise _session_not_found()
            self._require_owner(row, device_context)
            if row.is_forward_expired(self._clock()):
                raise _session_expired()
            return await self._hydrate_record(connection, row)

    async def _record_provider_part_once(
        self,
        *,
        session_id: MultipartUploadSessionId,
        part_number: int,
        etag: MultipartProviderPartETag,
        verified_size_bytes: int,
        device_context: SmallFileDeviceContext,
    ) -> None:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            row = await self._fetch_session_row(connection, session_id)
            if row is None:
                raise _session_not_found()
            self._require_owner(row, device_context)
            if not row.has_provider_identity():
                raise _state_invalid()
            if row.state not in _PART_RECORDING_STATES:
                raise _state_invalid()
            if row.is_forward_expired(self._clock()):
                raise _session_expired()
            geometry = row.geometry()
            try:
                window = geometry.part_range(part_number)
            except ValueError:
                raise _part_invalid() from None
            if verified_size_bytes != window.size_bytes:
                raise _provider_state_invalid()
            existing = await connection.execute(
                sa.select(
                    multipart_parts.c.provider_etag,
                    multipart_parts.c.verified_size_bytes,
                ).where(
                    multipart_parts.c.multipart_upload_id == row.multipart_upload_id,
                    multipart_parts.c.part_number == part_number,
                )
            )
            observed = existing.one_or_none()
            if observed is not None:
                if (
                    observed.provider_etag == etag.value
                    and int(observed.verified_size_bytes) == verified_size_bytes
                ):
                    return
                raise _provider_state_invalid()
            await connection.execute(
                multipart_part_insert_statement(
                    multipart_part_id=self._identity_generator(),
                    multipart_upload_id=row.multipart_upload_id,
                    part_number=part_number,
                    offset_bytes=window.offset_bytes,
                    size_bytes=window.size_bytes,
                    provider_etag_value=etag.value,
                    completed_at=self._require_aware_now(),
                )
            )
            if row.state is MultipartSessionState.CREATED:
                guarded = await connection.execute(
                    uploading_transition_statement(session_id_value=row.session_id_value)
                )
                if guarded.rowcount != 1:
                    raise _state_invalid()

    async def _claim_completion_once(
        self,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
    ) -> MultipartSessionClaim:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            row = await self._fetch_session_row(connection, session_id)
            if row is None:
                raise _session_not_found()
            self._require_owner(row, device_context)
            if not row.has_provider_identity():
                # A session whose provider create never completed cannot
                # claim the serialized completion.
                raise _state_invalid()
            if row.state is MultipartSessionState.COMMITTED:
                return MultipartSessionClaim(
                    session=await self._hydrate_record(connection, row),
                    claim_token=None,
                    claim_expires_at=None,
                )
            if row.state in _FORWARD_SESSION_STATES:
                if row.is_forward_expired(self._clock()):
                    raise _session_expired()
                claim_token = self._claim_token_generator()
                claim_expires_at = self._compute_completion_lease_expiry()
                if row.state in {MultipartSessionState.CREATED, MultipartSessionState.UPLOADING}:
                    guarded = await connection.execute(
                        completion_claim_transition_statement(
                            session_id_value=row.session_id_value,
                            claim_token=claim_token,
                            claim_expires_at=claim_expires_at,
                        )
                    )
                    if guarded.rowcount != 1:
                        raise _completion_in_progress()
                else:
                    if row.claim_expires_at is not None and row.claim_expires_at > self._clock():
                        raise _completion_in_progress()
                    guarded = await connection.execute(
                        completion_lease_replacement_statement(
                            session_id_value=row.session_id_value,
                            claim_token=claim_token,
                            claim_expires_at=claim_expires_at,
                            now=self._require_aware_now(),
                        )
                    )
                    if guarded.rowcount != 1:
                        raise _completion_in_progress()
                refreshed = await self._fetch_session_row(connection, session_id)
                if refreshed is None:  # pragma: no cover - the row is locked
                    raise _state_invalid()
                return MultipartSessionClaim(
                    session=await self._hydrate_record(connection, refreshed),
                    claim_token=claim_token,
                    claim_expires_at=claim_expires_at,
                )
            raise _state_invalid()

    async def _record_terminal_result_once(
        self,
        *,
        claim: MultipartSessionClaim,
        result: SmallFileTerminalResult | None,
        failure_state: MultipartSessionState | None,
    ) -> None:
        # The committed replay shape carries no fence to write under, so a
        # failure obligation can never arrive on it.
        if claim.claim_token is None and result is None:
            raise _state_invalid()
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            row = await self._fetch_session_row(
                connection, claim.session.session_id, for_update=True
            )
            if row is None:
                raise _session_not_found()
            if row.state is MultipartSessionState.COMMITTED:
                if result is not None and row.terminal_result() == result:
                    return
                raise _state_invalid()
            if row.state not in _COMPLETION_CLAIMED_STATES:
                raise _state_invalid()
            if claim.claim_token is None or row.claim_token != claim.claim_token:
                raise _completion_in_progress()
            if row.claim_expires_at is None or row.claim_expires_at <= self._clock():
                raise _completion_in_progress()
            if result is not None:
                guarded = await connection.execute(
                    terminal_result_update_statement(
                        session_id_value=row.session_id_value,
                        claim_token=claim.claim_token,
                        result=result,
                    )
                )
            else:
                assert failure_state is not None
                guarded = await connection.execute(
                    terminal_failure_update_statement(
                        session_id_value=row.session_id_value,
                        claim_token=claim.claim_token,
                        failure_state=failure_state,
                        now=self._require_aware_now(),
                    )
                )
            if guarded.rowcount != 1:
                raise _completion_in_progress()

    async def _claim_cleanup_batch_once(self, *, batch_limit: int) -> list[MultipartCleanupClaim]:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            now = self._require_aware_now()
            overdue = await connection.execute(
                expiry_sweep_select_statement(now=now, batch_limit=batch_limit)
            )
            struck_rows = list(overdue.mappings())
            for struck in struck_rows:
                await connection.execute(
                    expiry_strike_update_statement(session_id_value=struck["session_id"], now=now)
                )
            claimable = await connection.execute(
                cleanup_claim_select_statement(now=now, batch_limit=batch_limit)
            )
            claims: list[MultipartCleanupClaim] = []
            for row_mapping in list(claimable.mappings()):
                row = MultipartSessionRow.from_row_mapping(row_mapping)
                claim_token = self._claim_token_generator()
                claim_expires_at = self._compute_cleanup_lease_expiry()
                guarded = await connection.execute(
                    cleanup_claim_update_statement(
                        session_id_value=row.session_id_value,
                        claim_token=claim_token,
                        claim_expires_at=claim_expires_at,
                        now=now,
                    )
                )
                if guarded.rowcount != 1:  # pragma: no cover - the row is locked
                    continue
                refreshed = await self._fetch_session_row(
                    connection, MultipartUploadSessionId(row.session_id_value)
                )
                if refreshed is None:  # pragma: no cover - the row is locked
                    continue
                claims.append(
                    MultipartCleanupClaim(
                        session=await self._hydrate_record(connection, refreshed),
                        claim_token=claim_token,
                        claim_expires_at=claim_expires_at,
                    )
                )
            return claims

    async def _record_cleanup_result_once(
        self,
        *,
        claim: MultipartCleanupClaim,
        is_succeeded: bool,
        failure_reason: ErrorCode | None,
    ) -> None:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            row = await self._fetch_session_row(
                connection, claim.session.session_id, for_update=True
            )
            if row is None:
                raise _session_not_found()
            if row.claim_token != claim.claim_token:
                raise _state_invalid()
            if row.claim_expires_at is None or row.claim_expires_at <= self._clock():
                raise _state_invalid()
            now = self._require_aware_now()
            if row.cleanup_state not in _CLEANUP_UNFINISHED_STATES:
                raise _state_invalid()
            if is_succeeded:
                guarded = await connection.execute(
                    cleanup_success_update_statement(
                        session_id_value=row.session_id_value,
                        claim_token=claim.claim_token,
                        now=now,
                    )
                )
            else:
                assert failure_reason is not None
                completed_attempt_count = row.cleanup_attempt_count
                guarded = await connection.execute(
                    cleanup_failure_update_statement(
                        session_id_value=row.session_id_value,
                        claim_token=claim.claim_token,
                        failure_reason=failure_reason,
                        attempt_count=completed_attempt_count + 1,
                        next_retry_at=compute_cleanup_next_retry(
                            now, attempt_count=completed_attempt_count
                        ),
                        now=now,
                    )
                )
            if guarded.rowcount != 1:
                raise _state_invalid()

    async def _fetch_session_row(
        self,
        connection: AsyncConnection,
        session_id: MultipartUploadSessionId,
        *,
        for_update: bool = True,
    ) -> MultipartSessionRow | None:
        result = await connection.execute(
            multipart_session_select_statement(session_id, for_update=for_update)
        )
        row = result.mappings().one_or_none()
        return None if row is None else MultipartSessionRow.from_row_mapping(row)

    async def _fetch_session_row_by_operation(
        self, connection: AsyncConnection, operation_id: UUID
    ) -> MultipartSessionRow | None:
        result = await connection.execute(multipart_operation_select_statement(operation_id))
        row = result.mappings().one_or_none()
        return None if row is None else MultipartSessionRow.from_row_mapping(row)

    async def _fetch_operation_row(
        self, connection: AsyncConnection, operation_token: UploadOperationToken
    ) -> SmallFileOperationRow | None:
        result = await connection.execute(operation_token_lookup_statement(operation_token))
        row = result.mappings().one_or_none()
        return None if row is None else SmallFileOperationRow.from_row_mapping(row)

    async def _hydrate_record(
        self, connection: AsyncConnection, row: MultipartSessionRow
    ) -> MultipartSessionRecord:
        result = await connection.execute(
            sa.select(multipart_parts.c.part_number).where(
                multipart_parts.c.multipart_upload_id == row.multipart_upload_id
            )
        )
        completed_part_numbers = frozenset(int(entry[0]) for entry in result.all())
        return MultipartSessionRecord(
            session_id=MultipartUploadSessionId(row.session_id_value),
            state=row.state,
            part_size_bytes=row.part_size_bytes,
            part_count=row.part_count,
            total_size_bytes=row.declared_size_bytes,
            expires_at=row.expires_at,
            staging_key=row.staging_key,
            provider_upload_id=(
                None
                if row.provider_upload_id_value is None
                else MultipartProviderUploadId(row.provider_upload_id_value)
            ),
            completed_part_numbers=completed_part_numbers,
            terminal_result=row.terminal_result(),
        )

    def _require_owner(
        self, row: MultipartSessionRow, device_context: SmallFileDeviceContext
    ) -> None:
        if (
            row.workspace_id != device_context.workspace_id
            or row.device_id != device_context.device_id
        ):
            raise _session_not_found()

    def _require_aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return timezone-aware UTC moments")
        return now

    def _compute_session_expiry(self) -> datetime:
        return compute_multipart_session_expiry(self._require_aware_now())

    def _compute_completion_lease_expiry(self) -> datetime:
        return compute_completion_lease_expiry(self._require_aware_now())

    def _compute_cleanup_lease_expiry(self) -> datetime:
        return compute_cleanup_lease_expiry(self._require_aware_now())


class PostgresqlMultipartSessionEvidenceStore:
    """Durable frozen-evidence read of each multipart session.

    Implements the
    :class:`~personal_os.multipart_upload.service.MultipartSessionEvidenceStore`
    port over the same canonical baseline: one transaction resolves the
    owner-checked session row, opens its sealed raw-token preimage through
    the injected codec, proves that preimage against the frozen operation
    row's stored one-way token hash, then — under the shared
    operation-identity advisory lock, exactly like the small-file receive
    boundary — claims a still-``pending`` operation row into ``receiving``
    so the publication fence and its in-transaction terminal write accept
    the session. The 24-hour session deadline (already enforced by every
    caller of this port) governs the transfer lifetime; the small-file
    reservation's fifteen-minute deadline deliberately does not, because a
    multipart session resumes its frozen operation for up to twenty-four
    hours. A session with no seal (a composition without a codec, or a row
    predating migration ``20260828_04``) fails closed as the closed
    state-invalid error; no plaintext token is ever persisted, logged or
    rendered.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        token_codec: MultipartOperationTokenCodecPort,
        retry: MultipartDatabaseRetryPolicy | None = None,
    ) -> None:
        self._engine = engine
        self._token_codec = token_codec
        self._retry = retry if retry is not None else MultipartDatabaseRetryPolicy()

    async def load_bound_operation(
        self,
        *,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> BoundSmallFileOperation:
        """Resolve one owner-checked session's frozen bound operation."""

        del diagnostic_context
        return await self._retry.run(
            lambda _attempt: self._load_bound_operation_once(session_id, device_context)
        )

    async def _load_bound_operation_once(
        self,
        session_id: MultipartUploadSessionId,
        device_context: SmallFileDeviceContext,
    ) -> BoundSmallFileOperation:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            session_row = await self._fetch_session_row(connection, session_id)
            if session_row is None:
                raise _session_not_found()
            if (
                session_row.workspace_id != device_context.workspace_id
                or session_row.device_id != device_context.device_id
            ):
                raise _session_not_found()
            sealed = multipart_sealed_token_material(session_row)
            operation_row = await self._fetch_operation_row(connection, session_row.operation_id)
            if operation_row is None:
                raise _state_invalid()
            token = self._token_codec.open_token(sealed=sealed)
            if upload_operation_token_hash(token) != operation_row.operation_token_hash:
                # The sealed preimage drifted from the frozen operation row's
                # current token hash: unrecoverable evidence, never a guess.
                raise _state_invalid()
            # The shared operation-identity advisory lock serializes this
            # claim against every reservation rotation, exactly like the
            # small-file receive boundary; the lock-free re-read below
            # observes whatever the lock protects.
            await connection.execute(
                advisory_xact_lock_statement(
                    UPLOAD_OPERATION_LOCK_NAMESPACE,
                    upload_operation_identity_lock_key(
                        operation_row.workspace_id,
                        operation_row.device_id,
                        operation_row.event_id,
                        operation_row.idempotency_key,
                    ),
                )
            )
            operation_row = await self._fetch_operation_row(connection, session_row.operation_id)
            if operation_row is None:  # pragma: no cover - the row is locked
                raise _state_invalid()
            if operation_row.state == STATE_PENDING:
                claimed = await connection.execute(
                    receive_claim_statement(operation_id=operation_row.operation_id)
                )
                if claimed.rowcount != 1:
                    raise _state_invalid()
                operation_row = replace(operation_row, state=STATE_RECEIVING)
            elif operation_row.state == STATE_FAILED:
                raise _state_invalid()
            return _bound_operation_from_row(token, operation_row)

    async def _fetch_session_row(
        self, connection: AsyncConnection, session_id: MultipartUploadSessionId
    ) -> MultipartSessionRow | None:
        # Lock-free indexed lookup: the evidence read mutates only the
        # operation row, behind the operation-identity advisory lock.
        result = await connection.execute(
            multipart_session_select_statement(session_id, for_update=False)
        )
        row = result.mappings().one_or_none()
        return None if row is None else MultipartSessionRow.from_row_mapping(row)

    async def _fetch_operation_row(
        self, connection: AsyncConnection, operation_id: UUID
    ) -> SmallFileOperationRow | None:
        result = await connection.execute(operation_row_by_id_select_statement(operation_id))
        row = result.mappings().one_or_none()
        return None if row is None else SmallFileOperationRow.from_row_mapping(row)
