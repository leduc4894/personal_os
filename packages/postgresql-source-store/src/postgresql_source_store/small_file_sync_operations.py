"""Durable small-file upload-operation store over the canonical baseline.

:class:`PostgresqlSmallFileUploadOperationStore` implements the provider-neutral
:class:`~personal_os.small_file_sync.ports.SmallFileUploadOperationStore` port
against the ``20260818_01`` migration. ``resolve_terminal_result`` performs the
exact-replay lookup by the credential-derived device/workspace plus journal
event/idempotency identity (spec 10.3): a committed row returns its frozen
terminal canonical result unchanged — expiry never erases terminal evidence —
while any payload substitution under the same identity is the closed
identity-mismatch rejection. ``reserve_operation`` runs one ``READ COMMITTED``
transaction behind the operation-identity advisory lock and a ``SELECT ... FOR
UPDATE``: concurrent preflights for one identity converge on the single row
the unique constraint admits, a fresh reservation mints a new opaque URL-safe
token stored only as its one-way SHA-256 hash (a re-preflight rotates the
hash, so the raw token is never persisted or reused), a create reserves the
server-generated source UUID on the row without inserting any ``sources``
row, and an update records its base pair without reserving anything. An
expired non-terminal row is invalid for continuation — the receive-side
binding and the terminal write both refuse it — while a same-identity
re-preflight re-reserves it (fresh token, extended deadline): nothing was
committed for a non-terminal row, so re-reservation cannot double-publish.

``record_terminal_result`` persists the publication result and the operation's
terminal state as one guarded update inside a single transaction, and exposes
the same transition as :meth:`record_terminal_result_in_transaction
<PostgresqlSmallFileUploadOperationStore.record_terminal_result_in_transaction>`
so the publication service can drive its canonical writes and the terminal
operation state in one commit. The receive-side binding
(:meth:`resolve_bound_operation
<PostgresqlSmallFileUploadOperationStore.resolve_bound_operation>` and
:meth:`record_bound_terminal_result
<PostgresqlSmallFileUploadOperationStore.record_bound_terminal_result>`)
resolves one row by its one-way token hash — the raw token never exists in
storage — rechecks the credential-derived identity, state and expiry, and
applies the same guarded terminal transition over the token-bound view.
Every statement is schema-qualified and
parameter-bound; the adapter stores and logs no bytes, locator, token, receipt
or provider detail, and driver failures cross the boundary only through the
closed small-file error registry or ``internal_error``.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.exclusion_policy.enforcement import AllowedPolicyRevisionBinding
from personal_os.object_storage import CanonicalMediaType, ContentDigest
from personal_os.small_file_sync.contracts import (
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
    SmallFileUploadOperation,
    UploadOperationToken,
)
from personal_os.small_file_sync.errors import SmallFileSyncError
from personal_os.small_file_sync.ports import SmallFileBoundOperation
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import (
    RETRY_JITTER_MAXIMUM_SECONDS,
    RETRY_JITTER_MINIMUM_SECONDS,
    DatabaseFailureKind,
    classify_database_failure,
)
from postgresql_source_store.locks import advisory_xact_lock_statement, signed_first_sha256_word
from postgresql_source_store.tables import small_file_upload_operations

#: Server-owned operation lifetime: fifteen minutes from reservation. The
#: deadline is enforced at read/use time; terminal results survive it.
UPLOAD_OPERATION_EXPIRY_SECONDS: Final[int] = 900

#: Operation lock namespace (``"SFSO"`` ASCII) for upload-identity locks.
UPLOAD_OPERATION_LOCK_NAMESPACE: Final[int] = 0x5346534F

#: Closed operation-row states (implementation state, never client-visible).
STATE_PENDING: Final[str] = "pending"
STATE_RECEIVING: Final[str] = "receiving"
STATE_COMMITTED: Final[str] = "committed"
STATE_FAILED: Final[str] = "failed"

#: The non-terminal states an operation may be continued from.
_NON_TERMINAL_STATES: Final[frozenset[str]] = frozenset({STATE_PENDING, STATE_RECEIVING})

#: Entropy bytes behind every minted opaque operation token.
_OPERATION_TOKEN_ENTROPY_BYTES: Final[int] = 32

#: One row of the operation table: a SQLAlchemy row mapping from the
#: adapter's ``.mappings()`` results or an equivalent mapping in tests.
type _MappedRow = RowMapping | Mapping[str, Any]


def mint_upload_operation_token() -> UploadOperationToken:
    """Mint one fresh opaque URL-safe operation token.

    ``secrets.token_urlsafe(32)`` yields 43 printable base64url characters:
    within the domain grammar's 32-128 bound, never a raw canonical UUID, and
    never derived from any database identifier or object-store detail.
    """
    return UploadOperationToken(secrets.token_urlsafe(_OPERATION_TOKEN_ENTROPY_BYTES))


def upload_operation_token_hash(token: UploadOperationToken) -> str:
    """Return the one-way SHA-256 hex digest stored in place of the token."""
    return hashlib.sha256(token.value.encode("ascii")).hexdigest()


def upload_operation_identity_lock_key(
    workspace_id: UUID,
    device_id: UUID,
    event_id: UUID,
    idempotency_key_value: str,
) -> int:
    """Derive the transaction lock key from one operation identity's parts.

    The material is the workspace and device UUID bytes, the journal event UUID
    bytes and the exact idempotency-key bytes, each sealed by a NUL separator
    that cannot occur inside them. The receive-side binding carries the same
    identity as plain fields, so both paths derive one identical key.
    """
    material = b"\x00".join(
        (
            workspace_id.bytes,
            device_id.bytes,
            event_id.bytes,
            idempotency_key_value.encode("ascii"),
        )
    )
    return signed_first_sha256_word(material)


def upload_operation_lock_key(
    device_context: SmallFileDeviceContext, preflight: SmallFilePreflight
) -> int:
    """Derive the transaction lock key for one upload-operation identity."""

    return upload_operation_identity_lock_key(
        device_context.workspace_id,
        device_context.device_id,
        preflight.event_id,
        preflight.idempotency_key.value,
    )


def upload_operation_lock_statement(
    device_context: SmallFileDeviceContext, preflight: SmallFilePreflight
) -> sa.TextClause:
    """Build the transaction-scoped advisory lock for one operation identity."""
    return advisory_xact_lock_statement(
        UPLOAD_OPERATION_LOCK_NAMESPACE,
        upload_operation_lock_key(device_context, preflight),
    )


def compute_upload_operation_expiry(now: datetime) -> datetime:
    """Compute the operation deadline from the injectable aware-UTC clock."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC) + timedelta(seconds=UPLOAD_OPERATION_EXPIRY_SECONDS)


def operation_fingerprint_matches(
    row: _MappedRow, preflight: SmallFilePreflight, device_context: SmallFileDeviceContext
) -> bool:
    """Compare a stored operation row against the declared identity exactly.

    The comparison covers the device/workspace identity, the journal event and
    idempotency key and the complete declared payload fingerprint (operation
    kind, digest, exact byte size, canonical media type and the update
    source/base pair). Any divergence — including the same identity retrying
    with different bytes — fails, so no payload substitution is ever admitted.
    The accepted policy revision is deliberately not part of the fingerprint:
    a successful re-preflight rebinds the row to its newly allowed server
    revision while preserving the declared payload identity.
    """
    return (
        row["workspace_id"] == device_context.workspace_id
        and row["device_id"] == device_context.device_id
        and row["event_id"] == preflight.event_id
        and row["idempotency_key"] == preflight.idempotency_key.value
        and row["operation_kind"] == preflight.operation.value
        and row["declared_sha256"] == preflight.sha256.hexadecimal
        and int(row["declared_size_bytes"]) == preflight.size_bytes
        and row["declared_media_type"] == preflight.media_type.value
        and row["update_source_id"] == preflight.source_id
        and row["update_base_version_id"] == preflight.base_version_id
    )


_OPERATION_ROW_COLUMNS: Final[tuple[str, ...]] = (
    "operation_id",
    "operation_token_hash",
    "workspace_id",
    "device_id",
    "event_id",
    "idempotency_key",
    "operation_kind",
    "declared_sha256",
    "declared_size_bytes",
    "declared_media_type",
    "policy_revision_number",
    "reserved_source_id",
    "update_source_id",
    "update_base_version_id",
    "state",
    "safe_error_code",
    "result_kind",
    "result_source_id",
    "result_source_version_id",
    "result_content_version",
    "result_committed_at",
    "expires_at",
)


def _operation_row_select() -> sa.Select[tuple[Any, ...]]:
    """Build the schema-qualified full-column select of the operation table."""
    columns = [
        getattr(small_file_upload_operations.c, column_name)
        for column_name in _OPERATION_ROW_COLUMNS
    ]
    return sa.select(*columns)


def identity_lookup_statement(
    device_context: SmallFileDeviceContext,
    preflight: SmallFilePreflight,
    *,
    for_update: bool = True,
) -> sa.Select[tuple[Any, ...]]:
    """Build the schema-qualified identity lookup, optionally row-locked.

    ``for_update`` (the default) takes the identity row lock inside the
    reservation transaction; the replay lookup passes ``False`` for the
    lock-free indexed preflight.
    """
    statement = _operation_row_select().where(
        small_file_upload_operations.c.workspace_id == device_context.workspace_id,
        small_file_upload_operations.c.device_id == device_context.device_id,
        small_file_upload_operations.c.event_id == preflight.event_id,
        small_file_upload_operations.c.idempotency_key == preflight.idempotency_key.value,
    )
    return statement.with_for_update() if for_update else statement


def operation_insert_statement(
    *,
    operation_id: UUID,
    operation_token_hash: str,
    device_context: SmallFileDeviceContext,
    preflight: SmallFilePreflight,
    policy_revision_number: int,
    reserved_source_id: UUID | None,
    expires_at: datetime,
) -> sa.Insert:
    """Build the parameter-bound reservation insert of one pending operation."""
    return sa.insert(small_file_upload_operations).values(
        operation_id=operation_id,
        operation_token_hash=operation_token_hash,
        workspace_id=device_context.workspace_id,
        device_id=device_context.device_id,
        event_id=preflight.event_id,
        idempotency_key=preflight.idempotency_key.value,
        operation_kind=preflight.operation.value,
        declared_sha256=preflight.sha256.hexadecimal,
        declared_size_bytes=preflight.size_bytes,
        declared_media_type=preflight.media_type.value,
        policy_revision_number=policy_revision_number,
        reserved_source_id=reserved_source_id,
        update_source_id=preflight.source_id,
        update_base_version_id=preflight.base_version_id,
        state=STATE_PENDING,
        expires_at=expires_at,
    )


def operation_token_rotation_statement(
    *,
    operation_id: UUID,
    operation_token_hash: str,
    expires_at: datetime,
    policy_revision_number: int,
) -> sa.Update:
    """Build the guarded rotation of the stored token hash for one row.

    The same update writes the deadline the row now carries: a live row passes
    its own unchanged ``expires_at`` back, while the re-reservation of an
    expired non-terminal row passes its freshly computed extended deadline.
    """

    return (
        sa.update(small_file_upload_operations)
        .values(
            operation_token_hash=operation_token_hash,
            expires_at=expires_at,
            policy_revision_number=policy_revision_number,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(small_file_upload_operations.c.operation_id == operation_id)
    )


def terminal_result_update_statement(
    *, operation_id: UUID, result: SmallFileTerminalResult
) -> sa.Update:
    """Build the guarded terminal transition of one operation row.

    The guard admits exactly the non-terminal states; the frozen result and
    the terminal state land in one update, so no reader can observe a
    terminal state without its result or a result without its state.
    """
    return (
        sa.update(small_file_upload_operations)
        .values(
            state=STATE_COMMITTED,
            result_kind=result.result_kind.value,
            result_source_id=result.source_id,
            result_source_version_id=result.source_version_id,
            result_content_version=result.content_version,
            result_committed_at=result.committed_at,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            small_file_upload_operations.c.operation_id == operation_id,
            small_file_upload_operations.c.state.in_(_NON_TERMINAL_STATES),
        )
    )


def map_small_file_database_failure(cause: BaseException) -> ApplicationError:
    """Map a database or driver failure onto the closed error boundary.

    The small-file registry carries no dependency-unavailable code by design:
    transport-level retries belong to the plugin's bounded foreground retry
    (spec 12), never to the server request path. Every database failure —
    contention exhausted after the bounded retries, unavailability or an
    unclassified SQLSTATE — therefore crosses the boundary as the safe
    ``internal_error``, and a non-database exception is an internal bug of the
    same class. The cause remains chained only; its text never enters the
    error.
    """
    del cause
    return InternalApplicationError(ErrorCode.INTERNAL_ERROR)


@dataclass(frozen=True, slots=True)
class SmallFileOperationRow:
    """The hydrated view of one operation row used by the transitions."""

    operation_id: UUID
    operation_token_hash: str
    workspace_id: UUID
    device_id: UUID
    event_id: UUID
    idempotency_key: str
    operation_kind: str
    declared_sha256: str
    declared_size_bytes: int
    declared_media_type: str
    policy_revision_number: int
    reserved_source_id: UUID | None
    update_source_id: UUID | None
    update_base_version_id: UUID | None
    state: str
    safe_error_code: str | None
    result_kind: str | None
    result_source_id: UUID | None
    result_source_version_id: UUID | None
    result_content_version: int | None
    result_committed_at: datetime | None
    expires_at: datetime

    @classmethod
    def from_row_mapping(cls, row: _MappedRow) -> SmallFileOperationRow:
        """Build the typed row from one named result row of the lookup."""
        return cls(
            operation_id=row["operation_id"],
            operation_token_hash=row["operation_token_hash"],
            workspace_id=row["workspace_id"],
            device_id=row["device_id"],
            event_id=row["event_id"],
            idempotency_key=row["idempotency_key"],
            operation_kind=row["operation_kind"],
            declared_sha256=row["declared_sha256"],
            declared_size_bytes=int(row["declared_size_bytes"]),
            declared_media_type=row["declared_media_type"],
            policy_revision_number=int(row["policy_revision_number"]),
            reserved_source_id=row["reserved_source_id"],
            update_source_id=row["update_source_id"],
            update_base_version_id=row["update_base_version_id"],
            state=row["state"],
            safe_error_code=row["safe_error_code"],
            result_kind=row["result_kind"],
            result_source_id=row["result_source_id"],
            result_source_version_id=row["result_source_version_id"],
            result_content_version=(
                None
                if row["result_content_version"] is None
                else int(row["result_content_version"])
            ),
            result_committed_at=row["result_committed_at"],
            expires_at=row["expires_at"],
        )

    def terminal_result(self) -> SmallFileTerminalResult | None:
        """Hydrate the frozen terminal result of a committed row, if any.

        A committed row missing any result field is an integrity violation of
        the migration's terminal-shape CHECK and fails closed as the closed
        upload-state-invalid error, never as raw database evidence.
        """
        if self.state != STATE_COMMITTED:
            return None
        if (
            self.result_kind is None
            or self.result_source_id is None
            or self.result_source_version_id is None
            or self.result_content_version is None
            or self.result_committed_at is None
        ):
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        try:
            result_kind = SmallFileTerminalResultKind(self.result_kind)
        except ValueError:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID) from None
        return SmallFileTerminalResult(
            result_kind=result_kind,
            source_id=self.result_source_id,
            source_version_id=self.result_source_version_id,
            content_version=self.result_content_version,
            committed_at=self.result_committed_at,
        )


@dataclass(frozen=True, slots=True)
class SmallFileDatabaseRetryPolicy:
    """Bounded retry for the small-file store over the shared SQLSTATE classifier.

    At most ``maximum_attempts`` attempts run with the shared cancellable
    50-250 ms jitter. Typed application errors pass through untouched; every
    other failure — contention exhausted, unavailability or an unclassified
    database failure — maps through
    :func:`map_small_file_database_failure`, which never leaks SQLSTATE, SQL
    text, parameters or driver messages. No uncertain-commit recovery lookup
    is wired here: the terminal write is idempotent, so a lost commit
    acknowledgement resolves through the same-identity replay lookup of
    ``resolve_terminal_result`` on the next preflight.
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
            except ApplicationError:
                raise
            except Exception as cause:
                failure_kind = classify_database_failure(cause)
                if failure_kind is DatabaseFailureKind.NOT_DATABASE:
                    raise map_small_file_database_failure(cause) from cause
                if failure_kind is DatabaseFailureKind.CONTENTION:
                    if attempt == self.maximum_attempts:
                        raise map_small_file_database_failure(cause) from cause
                else:
                    raise map_small_file_database_failure(cause) from cause
                await sleep(jitter(RETRY_JITTER_MINIMUM_SECONDS, RETRY_JITTER_MAXIMUM_SECONDS))
        raise AssertionError("retry loop exhausted without a result")


def _identity_mismatch() -> SmallFileSyncError:
    return SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH)


def _operation_expired() -> SmallFileSyncError:
    return SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_EXPIRED)


def _state_invalid() -> SmallFileSyncError:
    return SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)


def _is_expired(expires_at: datetime, now: datetime) -> bool:
    return expires_at <= now


def _frozen_result_matches(
    row: SmallFileOperationRow, result: SmallFileTerminalResult
) -> bool:
    return (
        row.result_kind == result.result_kind.value
        and row.result_source_id == result.source_id
        and row.result_source_version_id == result.source_version_id
        and row.result_content_version == result.content_version
        and row.result_committed_at == result.committed_at
    )


def operation_token_lookup_statement(
    operation_token: UploadOperationToken,
) -> sa.Select[tuple[Any, ...]]:
    """Build the schema-qualified lookup of one operation row by token hash.

    The raw token is never stored or emitted: only its one-way SHA-256 hash
    crosses into the parameter-bound statement.
    """
    return _operation_row_select().where(
        small_file_upload_operations.c.operation_token_hash
        == upload_operation_token_hash(operation_token)
    )


def _bound_operation_from_row(
    operation_token: UploadOperationToken, row: SmallFileOperationRow
) -> SmallFileBoundOperation:
    """Hydrate the receive-side binding of one operation row.

    The caller's opaque token — the only place the raw token exists — rides
    along unchanged; every other member comes from the row. A value the row
    cannot re-parse (grammar drift) fails closed as the closed
    upload-state-invalid error, never as raw database evidence.
    """
    try:
        idempotency_key = SmallFileIdempotencyKey(row.idempotency_key)
        operation = SmallFileOperation(row.operation_kind)
        declared_sha256 = ContentDigest.parse(row.declared_sha256)
        declared_media_type = CanonicalMediaType.parse(row.declared_media_type)
    except ValueError:
        raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID) from None
    return SmallFileBoundOperation(
        operation_token=operation_token,
        workspace_id=row.workspace_id,
        device_id=row.device_id,
        event_id=row.event_id,
        idempotency_key=idempotency_key,
        operation=operation,
        declared_sha256=declared_sha256,
        declared_size_bytes=row.declared_size_bytes,
        declared_media_type=declared_media_type,
        policy_revision_number=row.policy_revision_number,
        reserved_source_id=row.reserved_source_id,
        update_source_id=row.update_source_id,
        update_base_version_id=row.update_base_version_id,
        expires_at=row.expires_at,
        terminal_result=row.terminal_result(),
    )


def _bound_matches_row(row: SmallFileOperationRow, bound: SmallFileBoundOperation) -> bool:
    """Compare a stored operation row against one receive-side binding exactly.

    The comparison covers the credential-derived identity and the complete
    declared fingerprint plus the create's reserved UUID, so a binding that
    drifted from its row — including any payload substitution — fails.
    """
    return (
        row.workspace_id == bound.workspace_id
        and row.device_id == bound.device_id
        and row.event_id == bound.event_id
        and row.idempotency_key == bound.idempotency_key.value
        and row.operation_kind == bound.operation.value
        and row.declared_sha256 == bound.declared_sha256.hexadecimal
        and int(row.declared_size_bytes) == bound.declared_size_bytes
        and row.declared_media_type == bound.declared_media_type.value
        and row.reserved_source_id == bound.reserved_source_id
        and row.update_source_id == bound.update_source_id
        and row.update_base_version_id == bound.update_base_version_id
        and int(row.policy_revision_number) == bound.policy_revision_number
    )


class PostgresqlSmallFileUploadOperationStore:
    """Durable upload-operation store over the canonical PostgreSQL baseline.

    Takes the composition-owned :class:`AsyncEngine` and the injectable aware
    UTC clock that owns expiry; it opens no connection at construction. The
    row lock order is fixed: the transaction-scoped operation-identity
    advisory lock, then the ``SELECT ... FOR UPDATE`` identity row, then the
    guarded updates.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        clock: Callable[[], datetime],
        retry: SmallFileDatabaseRetryPolicy | None = None,
        token_generator: Callable[[], UploadOperationToken] = mint_upload_operation_token,
        identity_generator: Callable[[], UUID] = uuid7,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._retry = retry if retry is not None else SmallFileDatabaseRetryPolicy()
        self._token_generator = token_generator
        self._identity_generator = identity_generator

    async def resolve_terminal_result(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileTerminalResult | None:
        del diagnostic_context
        return await self._retry.run(
            lambda _attempt: self._resolve_terminal_result_once(preflight, device_context)
        )

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
        return await self._retry.run(
            lambda _attempt: self._reserve_operation_once(
                preflight, device_context, policy_binding.policy_revision_number
            )
        )

    async def record_terminal_result(
        self,
        operation: SmallFileUploadOperation,
        result: SmallFileTerminalResult,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del diagnostic_context
        await self._retry.run(
            lambda _attempt: self._record_terminal_result_once(operation, result)
        )

    async def record_terminal_result_in_transaction(
        self,
        connection: AsyncConnection,
        operation: SmallFileUploadOperation,
        result: SmallFileTerminalResult,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        """Persist the terminal result on the caller's open transaction.

        The transactional seam the publication service drives: the guarded
        terminal update runs inside the caller's transaction (which is
        expected to hold the operation-identity advisory lock and to perform
        the canonical publication writes), so the publication result and the
        operation's terminal state commit — or roll back — together.
        """
        del diagnostic_context
        await self._apply_terminal_transition(connection, operation, result)

    async def resolve_bound_operation(
        self,
        operation_token: UploadOperationToken,
        device_context: SmallFileDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SmallFileBoundOperation:
        """Bind one receive to the exact operation its opaque token names.

        The lookup is by the one-way token hash only; the credential-derived
        workspace/device must then match the reserving identity exactly. A
        committed row carries its frozen terminal result so a response-loss
        replay returns it unchanged — terminal evidence survives expiry —
        while a non-terminal row past its deadline fails closed as the closed
        expired error and a failed row as the closed state-invalid error.
        """
        del diagnostic_context
        return await self._retry.run(
            lambda _attempt: self._resolve_bound_operation_once(
                operation_token, device_context
            )
        )

    async def record_bound_terminal_result(
        self,
        bound: SmallFileBoundOperation,
        result: SmallFileTerminalResult,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        """Persist the receive-side terminal result behind the identity lock.

        Mirrors :meth:`record_terminal_result` over the token-bound receive
        view: the operation-identity advisory lock, the guarded terminal
        update and the identical-result idempotence all apply, so a replayed
        or concurrent receive converges on the single frozen result.
        """
        del diagnostic_context
        await self._retry.run(
            lambda _attempt: self._record_bound_terminal_result_once(bound, result)
        )

    async def _resolve_bound_operation_once(
        self,
        operation_token: UploadOperationToken,
        device_context: SmallFileDeviceContext,
    ) -> SmallFileBoundOperation:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            result = await connection.execute(operation_token_lookup_statement(operation_token))
            row = result.mappings().one_or_none()
        if row is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND)
        hydrated = SmallFileOperationRow.from_row_mapping(row)
        if (
            hydrated.workspace_id != device_context.workspace_id
            or hydrated.device_id != device_context.device_id
        ):
            raise _identity_mismatch()
        bound = _bound_operation_from_row(operation_token, hydrated)
        if bound.terminal_result is not None:
            return bound
        if hydrated.state == STATE_FAILED:
            raise _state_invalid()
        if _is_expired(hydrated.expires_at, self._clock()):
            raise _operation_expired()
        return bound

    async def _record_bound_terminal_result_once(
        self, bound: SmallFileBoundOperation, result: SmallFileTerminalResult
    ) -> None:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            await connection.execute(
                advisory_xact_lock_statement(
                    UPLOAD_OPERATION_LOCK_NAMESPACE,
                    upload_operation_identity_lock_key(
                        bound.workspace_id,
                        bound.device_id,
                        bound.event_id,
                        bound.idempotency_key.value,
                    ),
                )
            )
            await self._apply_bound_terminal_transition(connection, bound, result)

    async def _apply_bound_terminal_transition(
        self,
        connection: AsyncConnection,
        bound: SmallFileBoundOperation,
        result: SmallFileTerminalResult,
    ) -> None:
        result_set = await connection.execute(
            operation_token_lookup_statement(bound.operation_token)
        )
        row_mapping = result_set.mappings().one_or_none()
        if row_mapping is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND)
        row = SmallFileOperationRow.from_row_mapping(row_mapping)
        if not _bound_matches_row(row, bound):
            raise _identity_mismatch()
        if row.state == STATE_COMMITTED:
            if _frozen_result_matches(row, result):
                return
            raise _state_invalid()
        if row.state == STATE_FAILED:
            raise _state_invalid()
        if _is_expired(row.expires_at, self._clock()):
            raise _operation_expired()
        guarded = await connection.execute(
            terminal_result_update_statement(operation_id=row.operation_id, result=result)
        )
        if guarded.rowcount != 1:
            raise _state_invalid()

    async def _resolve_terminal_result_once(
        self, preflight: SmallFilePreflight, device_context: SmallFileDeviceContext
    ) -> SmallFileTerminalResult | None:
        # Lock-free indexed lookup, mirroring the publication replay preflight:
        # the unique identity constraint alone decides presence.
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            result = await connection.execute(
                identity_lookup_statement(device_context, preflight, for_update=False)
            )
            row = result.mappings().one_or_none()
        if row is None:
            return None
        hydrated = SmallFileOperationRow.from_row_mapping(row)
        if not operation_fingerprint_matches(asdict(hydrated), preflight, device_context):
            raise _identity_mismatch()
        # Terminal results survive expiry: the deadline only ends continuation.
        return hydrated.terminal_result()

    async def _reserve_operation_once(
        self,
        preflight: SmallFilePreflight,
        device_context: SmallFileDeviceContext,
        policy_revision_number: int,
    ) -> SmallFileUploadOperation:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            await connection.execute(upload_operation_lock_statement(device_context, preflight))
            row = await self._fetch_identity_row(connection, device_context, preflight)
            if row is None:
                token = self._token_generator()
                reserved_source_id = (
                    self._identity_generator()
                    if preflight.operation is SmallFileOperation.CREATE
                    else None
                )
                expires_at = compute_upload_operation_expiry(self._clock())
                await connection.execute(
                    operation_insert_statement(
                        operation_id=self._identity_generator(),
                        operation_token_hash=upload_operation_token_hash(token),
                        device_context=device_context,
                        preflight=preflight,
                        policy_revision_number=policy_revision_number,
                        reserved_source_id=reserved_source_id,
                        expires_at=expires_at,
                    )
                )
                return SmallFileUploadOperation(
                    operation_token=token,
                    preflight=preflight,
                    device_context=device_context,
                    reserved_source_id=reserved_source_id,
                    expires_at=expires_at,
                )
            if not operation_fingerprint_matches(asdict(row), preflight, device_context):
                raise _identity_mismatch()
            if row.state not in _NON_TERMINAL_STATES:
                raise _state_invalid()
            # An expired non-terminal row re-reserves instead of refusing: no
            # terminal evidence exists for it (exact replay is unaffected —
            # terminal results survive expiry and are keyed by identity), and
            # nothing was committed, so rotating the token and extending the
            # deadline cannot double-publish. The pre-expiry token dies with
            # the rotation, and receive-time expiry checks keep refusing the
            # continuation of any token past its deadline.
            expires_at = row.expires_at
            if _is_expired(expires_at, self._clock()):
                expires_at = compute_upload_operation_expiry(self._clock())
            token = self._token_generator()
            await connection.execute(
                operation_token_rotation_statement(
                    operation_id=row.operation_id,
                    operation_token_hash=upload_operation_token_hash(token),
                    expires_at=expires_at,
                    policy_revision_number=policy_revision_number,
                )
            )
            return SmallFileUploadOperation(
                operation_token=token,
                preflight=preflight,
                device_context=device_context,
                reserved_source_id=row.reserved_source_id,
                expires_at=expires_at,
            )

    async def _record_terminal_result_once(
        self, operation: SmallFileUploadOperation, result: SmallFileTerminalResult
    ) -> None:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            await connection.execute(
                upload_operation_lock_statement(operation.device_context, operation.preflight)
            )
            await self._apply_terminal_transition(connection, operation, result)

    async def _apply_terminal_transition(
        self,
        connection: AsyncConnection,
        operation: SmallFileUploadOperation,
        result: SmallFileTerminalResult,
    ) -> None:
        result_set = await connection.execute(
            _operation_row_select().where(
                small_file_upload_operations.c.operation_token_hash
                == upload_operation_token_hash(operation.operation_token)
            )
        )
        row_mapping = result_set.mappings().one_or_none()
        if row_mapping is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND)
        row = SmallFileOperationRow.from_row_mapping(row_mapping)
        if not operation_fingerprint_matches(
            asdict(row), operation.preflight, operation.device_context
        ):
            raise _identity_mismatch()
        if row.state == STATE_COMMITTED:
            if _frozen_result_matches(row, result):
                return
            raise _state_invalid()
        if row.state == STATE_FAILED:
            raise _state_invalid()
        if _is_expired(row.expires_at, self._clock()):
            raise _operation_expired()
        guarded = await connection.execute(
            terminal_result_update_statement(operation_id=row.operation_id, result=result)
        )
        if guarded.rowcount != 1:
            raise _state_invalid()

    async def _fetch_identity_row(
        self,
        connection: AsyncConnection,
        device_context: SmallFileDeviceContext,
        preflight: SmallFilePreflight,
    ) -> SmallFileOperationRow | None:
        result = await connection.execute(identity_lookup_statement(device_context, preflight))
        row = result.mappings().one_or_none()
        return None if row is None else SmallFileOperationRow.from_row_mapping(row)
