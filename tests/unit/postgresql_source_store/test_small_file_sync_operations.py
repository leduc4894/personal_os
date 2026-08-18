"""Small-file upload-operation store statements, tokens, expiry and retry.

These tests pin the pure pieces of the PostgreSQL small-file sync adapter
without a database: the opaque operation-token minting and its one-way hash
storage (the raw token never reaches a column), the identity advisory-lock
derivation, the schema-qualified identity lookup and guarded terminal update
shapes, the payload-fingerprint comparison that permits no substitution, the
server-owned expiry computation, and the domain database retry policy whose
terminal mapping stays inside the closed small-file registry. The durable
transaction behavior is integration territory (disposable stack).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import sqlalchemy.exc as sa_exc
from sqlalchemy.dialects import postgresql

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.object_storage import CanonicalMediaType, ContentDigest
from personal_os.small_file_sync.contracts import (
    NormalizedLocator,
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFileTerminalResultKind,
    UploadOperationToken,
)
from personal_os.small_file_sync.errors import SmallFileSyncError
from postgresql_source_store.small_file_sync_operations import (
    UPLOAD_OPERATION_EXPIRY_SECONDS,
    SmallFileDatabaseRetryPolicy,
    compute_upload_operation_expiry,
    identity_lookup_statement,
    map_small_file_database_failure,
    mint_upload_operation_token,
    operation_fingerprint_matches,
    upload_operation_lock_key,
    upload_operation_lock_statement,
    upload_operation_token_hash,
)

_SENTINEL_STATEMENT = "SELECT do-not-emit-sql FROM knowledge.small_file_upload_operations"
_SENTINEL_DRIVER_TEXT = "do-not-emit-driver-text"
_DIGEST_A = ContentDigest.parse("a" * 64)
_DIGEST_B = ContentDigest.parse("b" * 64)
_POLICY_REVISION_NUMBER = 4


class _DriverFailure(Exception):
    """Fake driver exception carrying a SQLSTATE and sentinel driver text."""

    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(_SENTINEL_DRIVER_TEXT)
        self.sqlstate = sqlstate


def _contention_failure() -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("40P01"))


def _unclassified_failure() -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("23505"))


class _SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _preflight(
    *,
    operation: SmallFileOperation = SmallFileOperation.CREATE,
    sha256: ContentDigest = _DIGEST_A,
    size_bytes: int = 128,
    media_type: CanonicalMediaType | None = None,
    source_id: UUID | None = None,
    base_version_id: UUID | None = None,
) -> SmallFilePreflight:
    return SmallFilePreflight(
        event_id=uuid4(),
        idempotency_key=SmallFileIdempotencyKey(str(uuid4())),
        operation=operation,
        local_file_id=uuid4(),
        source_id=source_id if operation is SmallFileOperation.UPDATE else None,
        base_version_id=base_version_id if operation is SmallFileOperation.UPDATE else None,
        normalized_locator=NormalizedLocator("notes/daily/today.md"),
        sha256=sha256,
        size_bytes=size_bytes,
        media_type=(
            media_type if media_type is not None else CanonicalMediaType.parse("text/markdown")
        ),
        policy_revision_number=_POLICY_REVISION_NUMBER,
    )


def _device_context() -> SmallFileDeviceContext:
    return SmallFileDeviceContext(device_id=uuid4(), workspace_id=uuid4())


def _declared_fingerprint_row(
    preflight: SmallFilePreflight,
    device_context: SmallFileDeviceContext,
    *,
    operation_kind: str | None = None,
    declared_sha256: str | None = None,
    declared_size_bytes: int | None = None,
    declared_media_type: str | None = None,
    update_source_id: UUID | None = None,
    update_base_version_id: UUID | None = None,
) -> dict[str, Any]:
    return {
        "workspace_id": device_context.workspace_id,
        "device_id": device_context.device_id,
        "event_id": preflight.event_id,
        "idempotency_key": preflight.idempotency_key.value,
        "operation_kind": (
            preflight.operation.value if operation_kind is None else operation_kind
        ),
        "declared_sha256": (
            preflight.sha256.hexadecimal if declared_sha256 is None else declared_sha256
        ),
        "declared_size_bytes": (
            preflight.size_bytes if declared_size_bytes is None else declared_size_bytes
        ),
        "declared_media_type": (
            preflight.media_type.value if declared_media_type is None else declared_media_type
        ),
        "update_source_id": (
            preflight.source_id if update_source_id is None else update_source_id
        ),
        "update_base_version_id": (
            preflight.base_version_id if update_base_version_id is None else update_base_version_id
        ),
    }


# --- opaque token minting and one-way storage ------------------------------------


def test_minted_tokens_satisfy_the_opaque_grammar_and_are_unique() -> None:
    tokens = {mint_upload_operation_token().value for _ in range(64)}
    assert len(tokens) == 64
    for value in tokens:
        # Revalidation through the domain constructor proves the grammar.
        UploadOperationToken(value)


def test_token_hash_is_the_sha256_of_the_token_and_never_the_token_itself() -> None:
    token = mint_upload_operation_token()
    digest = upload_operation_token_hash(token)
    assert len(digest) == 64
    assert digest != token.value
    assert upload_operation_token_hash(token) == digest


# --- identity locking and lookup statement shapes ---------------------------------


def test_identity_lock_key_is_deterministic_and_identity_scoped() -> None:
    preflight = _preflight()
    device_context = _device_context()
    assert upload_operation_lock_key(device_context, preflight) == upload_operation_lock_key(
        device_context, preflight
    )
    other_device = SmallFileDeviceContext(
        device_id=uuid4(), workspace_id=device_context.workspace_id
    )
    assert upload_operation_lock_key(device_context, preflight) != upload_operation_lock_key(
        other_device, preflight
    )
    other_preflight = _preflight()
    assert upload_operation_lock_key(device_context, preflight) != upload_operation_lock_key(
        device_context, other_preflight
    )


def test_identity_lock_statement_is_a_bound_transaction_advisory_lock() -> None:
    statement = upload_operation_lock_statement(_device_context(), _preflight())
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "pg_advisory_xact_lock" in compiled
    assert compiled.count("%(namespace)s") == 1
    assert compiled.count("%(derived_key)s") == 1


def test_identity_lookup_statement_binds_the_full_identity_quadruple() -> None:
    device_context = _device_context()
    preflight = _preflight()
    statement = identity_lookup_statement(device_context, preflight)
    assert isinstance(statement, sa.Select)
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "knowledge.small_file_upload_operations" in compiled
    for column in (
        "workspace_id",
        "device_id",
        "event_id",
        "idempotency_key",
    ):
        assert f"small_file_upload_operations.{column} =" in compiled, column
    assert "FOR UPDATE" in compiled
    exported = {column.key for column in statement.exported_columns}
    assert "operation_id" in exported
    assert "operation_token_hash" in exported
    assert "reserved_source_id" in exported
    assert "state" in exported
    assert "expires_at" in exported
    assert "result_kind" in exported
    assert "result_committed_at" in exported


# --- payload fingerprint comparison ------------------------------------------------


def test_fingerprint_matches_only_the_exact_declared_payload() -> None:
    device_context = _device_context()
    preflight = _preflight()
    assert operation_fingerprint_matches(
        _declared_fingerprint_row(preflight, device_context), preflight, device_context
    )
    update_preflight = _preflight(
        operation=SmallFileOperation.UPDATE, source_id=uuid4(), base_version_id=uuid4()
    )
    assert operation_fingerprint_matches(
        _declared_fingerprint_row(update_preflight, device_context),
        update_preflight,
        device_context,
    )


@pytest.mark.parametrize(
    "mutator",
    [
        pytest.param(lambda row: {**row, "declared_sha256": _DIGEST_B.hexadecimal}, id="digest"),
        pytest.param(lambda row: {**row, "declared_size_bytes": 129}, id="size"),
        pytest.param(lambda row: {**row, "declared_media_type": "text/plain"}, id="media"),
        pytest.param(lambda row: {**row, "operation_kind": "update"}, id="kind"),
        pytest.param(lambda row: {**row, "event_id": uuid4()}, id="event"),
        pytest.param(lambda row: {**row, "idempotency_key": str(uuid4())}, id="key"),
    ],
)
def test_any_payload_substitution_fails_the_fingerprint_match(mutator: Any) -> None:
    preflight = _preflight()
    device_context = _device_context()
    substituted = mutator(_declared_fingerprint_row(preflight, device_context))
    assert not operation_fingerprint_matches(substituted, preflight, device_context)


def test_workspace_or_device_substitution_fails_the_identity_match() -> None:
    preflight = _preflight()
    device_context = _device_context()
    row = _declared_fingerprint_row(preflight, device_context)
    assert not operation_fingerprint_matches(
        {**row, "workspace_id": uuid4()}, preflight, device_context
    )
    assert not operation_fingerprint_matches(
        {**row, "device_id": uuid4()}, preflight, device_context
    )


# --- server-owned expiry -----------------------------------------------------------


def test_expiry_is_a_positive_bounded_offset_from_the_clock() -> None:
    assert 0 < UPLOAD_OPERATION_EXPIRY_SECONDS <= 3600
    now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
    expires_at = compute_upload_operation_expiry(now)
    assert expires_at == now + timedelta(seconds=UPLOAD_OPERATION_EXPIRY_SECONDS)
    assert expires_at.tzinfo is UTC


# --- domain database retry policy --------------------------------------------------


@pytest.mark.asyncio
async def test_retry_policy_retries_contention_then_succeeds() -> None:
    sleep = _SleepRecorder()
    attempts: list[int] = []

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        if len(attempts) < 3:
            raise _contention_failure()
        return "reserved"

    result = await SmallFileDatabaseRetryPolicy().run(
        operation, sleep=sleep, jitter=lambda minimum, maximum: minimum
    )
    assert result == "reserved"
    assert attempts == [1, 2, 3]
    assert len(sleep.delays) == 2


@pytest.mark.asyncio
async def test_retry_policy_passes_typed_application_errors_through() -> None:
    async def operation(attempt: int) -> None:
        raise SmallFileSyncError(ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND)

    with pytest.raises(SmallFileSyncError):
        await SmallFileDatabaseRetryPolicy().run(
            operation, sleep=_SleepRecorder(), jitter=lambda minimum, maximum: minimum
        )


def test_database_failure_mapping_never_leaks_driver_text() -> None:
    for cause in (
        _contention_failure(),
        _unclassified_failure(),
        RuntimeError(_SENTINEL_DRIVER_TEXT),
    ):
        error = map_small_file_database_failure(cause)
        rendered = f"{error!r} {error} {error.to_safe_dict()}"
        assert _SENTINEL_DRIVER_TEXT not in rendered
        assert _SENTINEL_STATEMENT not in rendered
        if isinstance(cause, sa_exc.DBAPIError):
            assert isinstance(error, InternalApplicationError)
            assert error.error_code is ErrorCode.INTERNAL_ERROR


# --- terminal result hydration contract --------------------------------------------


def test_terminal_result_kind_vocabulary_stays_domain_closed() -> None:
    # The adapter's stored kind tokens must equal the domain enum values so a
    # hydrated replay is exactly the frozen domain terminal result.
    assert {kind.value for kind in SmallFileTerminalResultKind} == {"committed", "no_change"}


def test_application_error_surface_is_small_file_or_internal() -> None:
    assert issubclass(SmallFileSyncError, ApplicationError)
    assert issubclass(InternalApplicationError, ApplicationError)
