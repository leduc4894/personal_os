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

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import sqlalchemy.exc as sa_exc
from sqlalchemy.dialects import postgresql

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.exclusion_policy.enforcement import AllowedPolicyRevisionBinding
from personal_os.object_storage import CanonicalMediaType, ContentDigest
from personal_os.small_file_sync.contracts import (
    BoundSmallFileOperation,
    NormalizedLocator,
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFileTerminalResultKind,
    UploadOperationToken,
    compute_locator_fingerprint,
)
from personal_os.small_file_sync.errors import SmallFileSyncError
from personal_os.small_file_sync.ports import SmallFileBoundOperation
from postgresql_source_store.small_file_sync_operations import (
    STATE_FAILED,
    STATE_PENDING,
    STATE_RECEIVING,
    UPLOAD_OPERATION_EXPIRY_SECONDS,
    PostgresqlSmallFileUploadOperationStore,
    SmallFileDatabaseRetryPolicy,
    SmallFileOperationRow,
    _bound_matches_row,
    bound_terminal_failure_update_statement,
    compute_upload_operation_expiry,
    identity_lookup_statement,
    locator_fingerprint_persisted,
    map_small_file_database_failure,
    mint_upload_operation_token,
    operation_fingerprint_matches,
    operation_insert_statement,
    operation_locator_fingerprint_column,
    operation_locator_rotation_statement,
    operation_token_rotation_statement,
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


def _policy_binding(
    device_context: SmallFileDeviceContext, revision: int
) -> AllowedPolicyRevisionBinding:
    return AllowedPolicyRevisionBinding(
        workspace_id=device_context.workspace_id, policy_revision_number=revision
    )


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
        "operation_kind": (preflight.operation.value if operation_kind is None else operation_kind),
        "declared_sha256": (
            preflight.sha256.hexadecimal if declared_sha256 is None else declared_sha256
        ),
        "declared_size_bytes": (
            preflight.size_bytes if declared_size_bytes is None else declared_size_bytes
        ),
        "declared_media_type": (
            preflight.media_type.value if declared_media_type is None else declared_media_type
        ),
        "update_source_id": (preflight.source_id if update_source_id is None else update_source_id),
        "update_base_version_id": (
            preflight.base_version_id if update_base_version_id is None else update_base_version_id
        ),
        "normalized_locator": preflight.normalized_locator.value,
        "locator_fingerprint": compute_locator_fingerprint(preflight.normalized_locator),
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


def test_insert_binds_the_server_policy_revision() -> None:
    device_context = _device_context()
    statement = operation_insert_statement(
        operation_id=uuid4(),
        operation_token_hash="a" * 64,
        device_context=device_context,
        preflight=_preflight(),
        policy_revision_number=7,
        reserved_source_id=uuid4(),
        expires_at=datetime.now(UTC),
    )

    assert statement.compile(dialect=postgresql.dialect()).params["policy_revision_number"] == 7


def test_token_rotation_rebinds_policy_revision_without_changing_fingerprint() -> None:
    device_context = _device_context()
    preflight = _preflight()
    statement = operation_token_rotation_statement(
        operation_id=uuid4(),
        operation_token_hash="a" * 64,
        expires_at=datetime.now(UTC),
        policy_revision_number=7,
    )

    assert statement.compile(dialect=postgresql.dialect()).params["policy_revision_number"] == 7
    assert operation_fingerprint_matches(
        {
            **_declared_fingerprint_row(preflight, device_context),
            "policy_revision_number": 2,
            "normalized_locator": preflight.normalized_locator.value,
            "locator_fingerprint": compute_locator_fingerprint(preflight.normalized_locator),
        },
        preflight,
        device_context,
    )


def test_bound_row_comparison_includes_policy_revision() -> None:
    device_context = _device_context()
    preflight = _preflight()
    row = SmallFileOperationRow(
        operation_id=uuid4(),
        operation_token_hash="a" * 64,
        workspace_id=device_context.workspace_id,
        device_id=device_context.device_id,
        event_id=preflight.event_id,
        idempotency_key=preflight.idempotency_key.value,
        operation_kind=preflight.operation.value,
        declared_sha256=preflight.sha256.hexadecimal,
        declared_size_bytes=preflight.size_bytes,
        declared_media_type=preflight.media_type.value,
        policy_revision_number=4,
        reserved_source_id=uuid4(),
        update_source_id=None,
        update_base_version_id=None,
        normalized_locator=None,
        locator_fingerprint=None,
        state="pending",
        safe_error_code=None,
        result_kind=None,
        result_source_id=None,
        result_source_version_id=None,
        result_content_version=None,
        result_committed_at=None,
        expires_at=datetime.now(UTC),
    )
    bound = SmallFileBoundOperation(
        operation_id=row.operation_id,
        operation_token=UploadOperationToken("A" * 43),
        workspace_id=row.workspace_id,
        device_id=row.device_id,
        event_id=row.event_id,
        idempotency_key=preflight.idempotency_key,
        operation=preflight.operation,
        declared_sha256=preflight.sha256,
        declared_size_bytes=preflight.size_bytes,
        declared_media_type=preflight.media_type,
        policy_revision_number=5,
        reserved_source_id=row.reserved_source_id,
        update_source_id=None,
        update_base_version_id=None,
        normalized_locator=None,
        locator_fingerprint=None,
        expires_at=row.expires_at,
        terminal_result=None,
    )

    assert not _bound_matches_row(row, bound)


class _NoSqlEngine:
    def __init__(self) -> None:
        self.was_entered = False

    def connect(self) -> None:
        self.was_entered = True
        raise AssertionError("foreign binding must be rejected before SQL")


@pytest.mark.asyncio
async def test_reservation_rejects_a_foreign_workspace_binding_before_sql() -> None:
    engine = _NoSqlEngine()
    store = PostgresqlSmallFileUploadOperationStore(engine, clock=lambda: datetime.now(UTC))  # type: ignore[arg-type]
    device_context = _device_context()

    with pytest.raises(SmallFileSyncError) as rejected:
        await store.reserve_operation(
            _preflight(),
            device_context,
            _policy_binding(
                SmallFileDeviceContext(device_id=device_context.device_id, workspace_id=uuid4()), 7
            ),
            None,  # type: ignore[arg-type]
        )

    assert rejected.value.error_code is ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID
    assert not engine.was_entered


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


# --- initial locator persistence (task 3) ----------------------------------------


def test_insert_statement_binds_normalized_locator_and_its_digest() -> None:
    device_context = _device_context()
    preflight = _preflight()
    locator = preflight.normalized_locator
    digest = compute_locator_fingerprint(locator)

    statement = operation_insert_statement(
        operation_id=uuid4(),
        operation_token_hash="a" * 64,
        device_context=device_context,
        preflight=preflight,
        policy_revision_number=7,
        reserved_source_id=uuid4(),
        expires_at=datetime.now(UTC),
        normalized_locator=locator,
        locator_fingerprint=digest,
    )

    compiled = statement.compile(dialect=postgresql.dialect())
    params = compiled.params
    assert params["normalized_locator"] == locator.value
    assert params["locator_fingerprint"] == digest


def test_insert_statement_accepts_null_locator_for_legacy_pre_migration_rows() -> None:
    device_context = _device_context()
    preflight = _preflight()

    statement = operation_insert_statement(
        operation_id=uuid4(),
        operation_token_hash="a" * 64,
        device_context=device_context,
        preflight=preflight,
        policy_revision_number=7,
        reserved_source_id=uuid4(),
        expires_at=datetime.now(UTC),
        normalized_locator=None,
        locator_fingerprint=None,
    )

    compiled = statement.compile(dialect=postgresql.dialect())
    params = compiled.params
    assert params["normalized_locator"] is None
    assert params["locator_fingerprint"] is None


def test_locator_fingerprint_persisted_predicate_compares_digest_only() -> None:
    """Replay compares the locator fingerprint, not the raw locator string."""

    locator = NormalizedLocator("notes/planning.md")
    other = NormalizedLocator("notes/other.md")
    digest = compute_locator_fingerprint(locator)

    # The stored digest matches — the raw locator may or may not be present.
    assert locator_fingerprint_persisted(digest, locator.value)
    assert locator_fingerprint_persisted(digest, None)
    # A different locator does not match the stored digest.
    assert not locator_fingerprint_persisted(digest, other.value)
    # A stored None digest only matches a None locator.
    assert locator_fingerprint_persisted(None, None)
    assert not locator_fingerprint_persisted(None, locator.value)


def test_operation_row_exposes_locator_evidence_field() -> None:
    """The locator evidence column is part of the hydrated operation row."""

    assert operation_locator_fingerprint_column() == "locator_fingerprint"


def test_rotation_statement_preserves_locator_fingerprint_when_token_rebinds() -> None:
    """Pending re-preflight may rotate the token without disturbing the digest."""

    preflight = _preflight()
    locator = preflight.normalized_locator
    digest = compute_locator_fingerprint(locator)

    statement = operation_locator_rotation_statement(
        operation_id=uuid4(),
        operation_token_hash="a" * 64,
        expires_at=datetime.now(UTC),
        policy_revision_number=7,
        locator_fingerprint=digest,
    )

    compiled = statement.compile(dialect=postgresql.dialect())
    params = compiled.params
    assert params["locator_fingerprint"] == digest


def test_bound_operation_comparison_includes_locator_fingerprint() -> None:
    """Replay matches the locator fingerprint so raw locator drift is detected."""

    device_context = _device_context()
    preflight = _preflight()
    locator = preflight.normalized_locator
    digest = compute_locator_fingerprint(locator)

    row = SmallFileOperationRow(
        operation_id=uuid4(),
        operation_token_hash="a" * 64,
        workspace_id=device_context.workspace_id,
        device_id=device_context.device_id,
        event_id=preflight.event_id,
        idempotency_key=preflight.idempotency_key.value,
        operation_kind=preflight.operation.value,
        declared_sha256=preflight.sha256.hexadecimal,
        declared_size_bytes=preflight.size_bytes,
        declared_media_type=preflight.media_type.value,
        policy_revision_number=4,
        reserved_source_id=uuid4(),
        update_source_id=None,
        update_base_version_id=None,
        state="pending",
        safe_error_code=None,
        result_kind=None,
        result_source_id=None,
        result_source_version_id=None,
        result_content_version=None,
        result_committed_at=None,
        expires_at=datetime.now(UTC),
        normalized_locator=locator.value,
        locator_fingerprint=digest,
    )
    bound = BoundSmallFileOperation(
        operation_id=row.operation_id,
        operation_token=UploadOperationToken("A" * 43),
        workspace_id=row.workspace_id,
        device_id=row.device_id,
        event_id=row.event_id,
        idempotency_key=preflight.idempotency_key,
        operation=preflight.operation,
        declared_sha256=preflight.sha256,
        declared_size_bytes=preflight.size_bytes,
        declared_media_type=preflight.media_type,
        policy_revision_number=4,
        reserved_source_id=row.reserved_source_id,
        update_source_id=None,
        update_base_version_id=None,
        normalized_locator=locator,
        locator_fingerprint=digest,
        expires_at=row.expires_at,
        terminal_result=None,
    )

    assert _bound_matches_row(row, bound)


def test_bound_operation_comparison_rejects_digest_mismatch() -> None:
    """A drifted locator digest makes the binding fail closed."""

    device_context = _device_context()
    preflight = _preflight()
    locator = preflight.normalized_locator
    digest = compute_locator_fingerprint(locator)
    other_locator = NormalizedLocator("notes/other.md")
    other_digest = compute_locator_fingerprint(other_locator)

    row = SmallFileOperationRow(
        operation_id=uuid4(),
        operation_token_hash="a" * 64,
        workspace_id=device_context.workspace_id,
        device_id=device_context.device_id,
        event_id=preflight.event_id,
        idempotency_key=preflight.idempotency_key.value,
        operation_kind=preflight.operation.value,
        declared_sha256=preflight.sha256.hexadecimal,
        declared_size_bytes=preflight.size_bytes,
        declared_media_type=preflight.media_type.value,
        policy_revision_number=4,
        reserved_source_id=uuid4(),
        update_source_id=None,
        update_base_version_id=None,
        state="pending",
        safe_error_code=None,
        result_kind=None,
        result_source_id=None,
        result_source_version_id=None,
        result_content_version=None,
        result_committed_at=None,
        expires_at=datetime.now(UTC),
        normalized_locator=locator.value,
        locator_fingerprint=digest,
    )
    bound = BoundSmallFileOperation(
        operation_id=row.operation_id,
        operation_token=UploadOperationToken("A" * 43),
        workspace_id=row.workspace_id,
        device_id=row.device_id,
        event_id=row.event_id,
        idempotency_key=preflight.idempotency_key,
        operation=preflight.operation,
        declared_sha256=preflight.sha256,
        declared_size_bytes=preflight.size_bytes,
        declared_media_type=preflight.media_type,
        policy_revision_number=4,
        reserved_source_id=row.reserved_source_id,
        update_source_id=None,
        update_base_version_id=None,
        # The bound carries a different locator with its own digest, so the
        # locator_fingerprint_persisted comparison must reject it.
        normalized_locator=other_locator,
        locator_fingerprint=other_digest,
        expires_at=row.expires_at,
        terminal_result=None,
    )

    assert not _bound_matches_row(row, bound)


# --- durable update receive over the real binding path ---------------------------


class _ScriptedResult:
    """Minimal async result double for one scripted statement execution."""

    def __init__(self, *, mapping: dict[str, Any] | None = None, rowcount: int = 0) -> None:
        self._mapping = mapping
        self.rowcount = rowcount

    def mappings(self) -> _ScriptedResult:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._mapping


class _ReceiveScriptedConnection:
    """Connection double serving one durable operation-row view by token."""

    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row
        self.claim_executed = False

    def begin(self) -> _ScriptedBegin:
        return _ScriptedBegin()

    async def execute(self, statement: Any) -> _ScriptedResult:
        visit_name = statement.__visit_name__
        if visit_name == "select":
            compiled = str(statement.compile())
            if "operation_token_hash" in compiled:
                return _ScriptedResult(mapping=self._row)
            raise AssertionError(f"unexpected select: {compiled}")
        if visit_name == "update":
            self.claim_executed = True
            return _ScriptedResult(rowcount=1)
        if visit_name in {"text", "textclause"}:
            return _ScriptedResult()
        raise AssertionError(f"unexpected statement kind: {visit_name}")


class _ScriptedBegin:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _ReceiveScriptedEngine:
    def __init__(self, connection: _ReceiveScriptedConnection) -> None:
        self._connection = connection

    def connect(self) -> _ReceiveScriptedContext:
        return _ReceiveScriptedContext(self._connection)


class _ReceiveScriptedContext:
    def __init__(self, connection: _ReceiveScriptedConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _ReceiveScriptedConnection:
        return self._connection

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def _durable_update_row_with_persisted_locator(
    preflight: SmallFilePreflight, device_context: SmallFileDeviceContext
) -> dict[str, Any]:
    """One claimed update row carrying the update preflight's locator evidence.

    This is the exact durable geometry the live first-ever update produced:
    the reservation persisted both the raw ``normalized_locator`` the update
    preflight declared and its retained digest, because the update preflight
    evaluates locator policy evidence server-side.
    """

    return {
        "operation_id": uuid4(),
        "operation_token_hash": "c" * 64,
        "workspace_id": device_context.workspace_id,
        "device_id": device_context.device_id,
        "event_id": preflight.event_id,
        "idempotency_key": preflight.idempotency_key.value,
        "operation_kind": SmallFileOperation.UPDATE.value,
        "declared_sha256": preflight.sha256.hexadecimal,
        "declared_size_bytes": preflight.size_bytes,
        "declared_media_type": preflight.media_type.value,
        "policy_revision_number": _POLICY_REVISION_NUMBER,
        "reserved_source_id": None,
        "update_source_id": preflight.source_id,
        "update_base_version_id": preflight.base_version_id,
        "normalized_locator": preflight.normalized_locator.value
        if preflight.normalized_locator is not None
        else None,
        "locator_fingerprint": compute_locator_fingerprint(preflight.normalized_locator)
        if preflight.normalized_locator is not None
        else None,
        "state": "pending",
        "safe_error_code": None,
        "result_kind": None,
        "result_source_id": None,
        "result_source_version_id": None,
        "result_content_version": None,
        "result_committed_at": None,
        "expires_at": datetime.now(UTC) + timedelta(seconds=UPLOAD_OPERATION_EXPIRY_SECONDS),
    }


@pytest.mark.asyncio
async def test_receive_binding_ignores_persisted_raw_locator_on_update_rows() -> None:
    """A claimed update row binds without its raw locator (the live 500).

    The durable reservation persisted the update preflight's raw locator, so
    hydrating it onto the receive binding violated the closed
    ``BoundSmallFileOperation`` contract (an update must never carry a
    normalized locator). The ``ValueError`` escaped as the registry's closed
    ``internal_error`` and every content-upload retry answered HTTP 500. The
    binding must surface only the retained digest for an update row.
    """

    device_context = _device_context()
    preflight = _preflight(
        operation=SmallFileOperation.UPDATE, source_id=uuid4(), base_version_id=uuid4()
    )
    assert preflight.normalized_locator is not None
    connection = _ReceiveScriptedConnection(
        _durable_update_row_with_persisted_locator(preflight, device_context)
    )
    store = PostgresqlSmallFileUploadOperationStore(
        cast(Any, _ReceiveScriptedEngine(connection)), clock=lambda: datetime.now(UTC)
    )

    bound = await store.resolve_bound_operation(
        UploadOperationToken("B" * 43), device_context, cast(Any, object())
    )

    assert bound.operation is SmallFileOperation.UPDATE
    assert bound.normalized_locator is None
    assert bound.locator_fingerprint == compute_locator_fingerprint(preflight.normalized_locator)
    assert bound.update_source_id == preflight.source_id
    assert bound.update_base_version_id == preflight.base_version_id
    assert connection.claim_executed is True


class _ReserveScriptedConnection:
    """Connection double serving an empty identity view and the insert."""

    def __init__(self) -> None:
        self.insert_statement: Any = None

    def begin(self) -> _ScriptedBegin:
        return _ScriptedBegin()

    async def execute(self, statement: Any) -> _ScriptedResult:
        visit_name = statement.__visit_name__
        if visit_name == "select":
            return _ScriptedResult(mapping=None)
        if visit_name == "insert":
            self.insert_statement = statement
            return _ScriptedResult(rowcount=1)
        if visit_name in {"text", "textclause"}:
            return _ScriptedResult()
        raise AssertionError(f"unexpected statement kind: {visit_name}")


@pytest.mark.asyncio
async def test_reservation_persists_only_the_locator_digest_for_update_preflights() -> None:
    """An update reservation never persists the raw locator column.

    The raw locator column is the create's bound initial-locator evidence the
    receive binding carries into the publication transaction; an update's
    locator is preflight policy evidence only, so the reservation retains the
    one-way digest (the replay identity of the declared locator) and binds
    ``normalized_locator`` to NULL.
    """

    device_context = _device_context()
    preflight = _preflight(
        operation=SmallFileOperation.UPDATE, source_id=uuid4(), base_version_id=uuid4()
    )
    assert preflight.normalized_locator is not None
    connection = _ReserveScriptedConnection()
    store = PostgresqlSmallFileUploadOperationStore(
        cast(Any, _ReceiveScriptedEngine(connection)), clock=lambda: datetime.now(UTC)
    )

    operation = await store.reserve_operation(
        preflight,
        device_context,
        _policy_binding(device_context, _POLICY_REVISION_NUMBER),
        cast(Any, object()),
    )

    assert operation.operation_token is not None
    assert connection.insert_statement is not None
    params = connection.insert_statement.compile(dialect=postgresql.dialect()).params
    assert params["normalized_locator"] is None
    assert params["locator_fingerprint"] == compute_locator_fingerprint(
        preflight.normalized_locator
    )


# --- typed-rejection terminalization of a claimed receive (task 1 remediation) ------


def test_bound_terminal_failure_statement_writes_the_registry_token_only() -> None:
    """The guarded failure write lands the closed token over the receiving guard.

    The UPDATE's SET clause carries only the terminal state, the closed
    registry token and the server timestamp; the WHERE clause admits exactly
    one operation row still in ``receiving``, so a concurrent terminal
    winner is visible as a zero-row guarded update.
    """

    statement = bound_terminal_failure_update_statement(
        operation_id=uuid4(), error_code=ErrorCode.SOURCE_LOCATOR_CONFLICT
    )

    params = statement.compile(dialect=postgresql.dialect()).params
    assert params["state"] == STATE_FAILED
    assert params["safe_error_code"] == "source_locator_conflict"
    rendered = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "knowledge.small_file_upload_operations" in rendered
    assert f"state = '{STATE_RECEIVING}'" in rendered
    assert "updated_at=CURRENT_TIMESTAMP" in rendered


def _receiving_create_row_and_bound(
    *,
    state: str = STATE_RECEIVING,
    safe_error_code: str | None = None,
) -> tuple[dict[str, Any], SmallFileBoundOperation]:
    """One claimed create row and its exact receive-side binding."""

    device_context = _device_context()
    preflight = _preflight()
    locator = preflight.normalized_locator
    assert locator is not None
    digest = compute_locator_fingerprint(locator)
    reserved_source_id = uuid4()
    expires_at = datetime.now(UTC) + timedelta(seconds=UPLOAD_OPERATION_EXPIRY_SECONDS)
    row: dict[str, Any] = {
        "operation_id": uuid4(),
        "operation_token_hash": "d" * 64,
        "workspace_id": device_context.workspace_id,
        "device_id": device_context.device_id,
        "event_id": preflight.event_id,
        "idempotency_key": preflight.idempotency_key.value,
        "operation_kind": SmallFileOperation.CREATE.value,
        "declared_sha256": preflight.sha256.hexadecimal,
        "declared_size_bytes": preflight.size_bytes,
        "declared_media_type": preflight.media_type.value,
        "policy_revision_number": _POLICY_REVISION_NUMBER,
        "reserved_source_id": reserved_source_id,
        "update_source_id": None,
        "update_base_version_id": None,
        "normalized_locator": locator.value,
        "locator_fingerprint": digest,
        "state": state,
        "safe_error_code": safe_error_code,
        "result_kind": None,
        "result_source_id": None,
        "result_source_version_id": None,
        "result_content_version": None,
        "result_committed_at": None,
        "expires_at": expires_at,
    }
    bound = SmallFileBoundOperation(
        operation_id=row["operation_id"],
        operation_token=UploadOperationToken("D" * 43),
        workspace_id=device_context.workspace_id,
        device_id=device_context.device_id,
        event_id=preflight.event_id,
        idempotency_key=preflight.idempotency_key,
        operation=SmallFileOperation.CREATE,
        declared_sha256=preflight.sha256,
        declared_size_bytes=preflight.size_bytes,
        declared_media_type=preflight.media_type,
        policy_revision_number=_POLICY_REVISION_NUMBER,
        reserved_source_id=reserved_source_id,
        update_source_id=None,
        update_base_version_id=None,
        normalized_locator=locator,
        locator_fingerprint=digest,
        expires_at=expires_at,
        terminal_result=None,
    )
    return row, bound


class _TerminalFailureScriptedConnection:
    """Connection double serving one claimed row and applying its failure write.

    The double serves the durable row for every token-hash lookup and folds
    the guarded locator-clear and typed-failure updates back onto its own row
    view, so a replayed call observes the terminal failure state it
    previously wrote instead of a second write.
    """

    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row
        self.operation_state: str | None = None
        self.safe_error_code: str | None = None
        self.failed_write_count = 0
        self.locator_clear_count = 0

    def begin(self) -> _ScriptedBegin:
        return _ScriptedBegin()

    async def execute(self, statement: Any) -> _ScriptedResult:
        visit_name = statement.__visit_name__
        if visit_name == "select":
            compiled = str(statement.compile())
            if "operation_token_hash" in compiled:
                return _ScriptedResult(mapping=self._row)
            raise AssertionError(f"unexpected select: {compiled}")
        if visit_name == "update":
            params = statement.compile(dialect=postgresql.dialect()).params
            if "safe_error_code" not in params:
                # The guarded locator clear runs first while the row is still
                # in receiving: fold the nulled raw locator onto the row view.
                assert params["normalized_locator"] is None
                self.locator_clear_count += 1
                self._row["normalized_locator"] = None
                return _ScriptedResult(rowcount=1)
            self.failed_write_count += 1
            self.operation_state = params["state"]
            self.safe_error_code = params["safe_error_code"]
            self._row["state"] = params["state"]
            self._row["safe_error_code"] = params["safe_error_code"]
            return _ScriptedResult(rowcount=1)
        if visit_name in {"text", "textclause"}:
            return _ScriptedResult()
        raise AssertionError(f"unexpected statement kind: {visit_name}")


def _terminal_failure_store(
    connection: _TerminalFailureScriptedConnection,
) -> PostgresqlSmallFileUploadOperationStore:
    return PostgresqlSmallFileUploadOperationStore(
        cast(Any, _ReceiveScriptedEngine(connection)), clock=lambda: datetime.now(UTC)
    )


@pytest.mark.asyncio
async def test_typed_rejection_moves_receiving_operation_to_failed() -> None:
    """A typed business rejection terminalizes the claimed receive row.

    The typed 409 the publication boundary raises (for example the guarded
    locator conflict) must never leave the canonical operation row fenced in
    ``receiving``: the guarded write lands ``failed`` carrying only the
    closed registry token, behind the same operation-identity fence the
    terminal-result write uses.
    """

    row, bound = _receiving_create_row_and_bound()
    connection = _TerminalFailureScriptedConnection(row)

    await _terminal_failure_store(connection).record_bound_terminal_failure(
        bound, ErrorCode.SOURCE_LOCATOR_CONFLICT, cast(Any, object())
    )

    assert connection.operation_state == STATE_FAILED
    assert connection.safe_error_code == "source_locator_conflict"
    # The failure transition clears the raw locator exactly like the success
    # transition, before the guarded failure write lands.
    assert connection.locator_clear_count == 1
    assert row["normalized_locator"] is None


@pytest.mark.asyncio
async def test_terminal_failure_replay_is_idempotent() -> None:
    """Replaying the identical bound/code pair writes the failure exactly once."""

    row, bound = _receiving_create_row_and_bound()
    connection = _TerminalFailureScriptedConnection(row)
    store = _terminal_failure_store(connection)

    await store.record_bound_terminal_failure(
        bound, ErrorCode.SOURCE_LOCATOR_CONFLICT, cast(Any, object())
    )
    await store.record_bound_terminal_failure(
        bound, ErrorCode.SOURCE_LOCATOR_CONFLICT, cast(Any, object())
    )

    assert connection.failed_write_count == 1
    assert connection.locator_clear_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "safe_error_code", "error_code"),
    [
        pytest.param(
            STATE_FAILED,
            "small_file_upload_state_invalid",
            ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED,
            id="failed row with a different code",
        ),
        pytest.param(
            "committed",
            None,
            ErrorCode.SOURCE_LOCATOR_CONFLICT,
            id="committed row",
        ),
        pytest.param(
            "pending",
            None,
            ErrorCode.SOURCE_LOCATOR_CONFLICT,
            id="unclaimed row",
        ),
    ],
)
async def test_terminal_failure_rejects_other_prior_records(
    state: str, safe_error_code: str | None, error_code: ErrorCode
) -> None:
    """Only the identical bound/code pair replays; every other record fails closed.

    A failed row already carrying a different closed token, a committed row
    and an unclaimed pending row are all prior terminal-or-invalid records:
    each surfaces the existing closed upload-state-invalid error without any
    new write, mirroring the fence of the terminal-result transition.
    """

    row, bound = _receiving_create_row_and_bound(state=state, safe_error_code=safe_error_code)
    connection = _TerminalFailureScriptedConnection(row)

    with pytest.raises(SmallFileSyncError) as rejected:
        await _terminal_failure_store(connection).record_bound_terminal_failure(
            bound, error_code, cast(Any, object())
        )

    assert rejected.value.error_code is ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID
    assert connection.failed_write_count == 0


@pytest.mark.asyncio
async def test_terminal_failure_rejects_a_drifted_binding() -> None:
    """A binding that no longer matches its row fails closed as identity mismatch."""

    row, bound = _receiving_create_row_and_bound()
    row["declared_size_bytes"] = bound.declared_size_bytes + 1
    connection = _TerminalFailureScriptedConnection(row)

    with pytest.raises(SmallFileSyncError) as rejected:
        await _terminal_failure_store(connection).record_bound_terminal_failure(
            bound, ErrorCode.SOURCE_LOCATOR_CONFLICT, cast(Any, object())
        )

    assert rejected.value.error_code is ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH
    assert connection.failed_write_count == 0


# --- reclaiming a terminal-failed row through same-identity re-preflight -----------

#: The closed token a typed rejection lands on the failed row.
_FAILED_SAFE_ERROR_CODE = "source_locator_conflict"

#: The raw token of the superseded claim the failed row still fences.
_SUPERSEDED_RAW_TOKEN = UploadOperationToken("E" * 43)


def _reclaim_row_and_preflight(
    *,
    state: str,
    safe_error_code: str | None,
    expires_at: datetime,
) -> tuple[dict[str, Any], SmallFilePreflight, SmallFileDeviceContext]:
    """One identity-matched create row and the exact preflight that reserved it."""

    device_context = _device_context()
    preflight = _preflight()
    locator = preflight.normalized_locator
    assert locator is not None
    row: dict[str, Any] = {
        "operation_id": uuid4(),
        "operation_token_hash": upload_operation_token_hash(_SUPERSEDED_RAW_TOKEN),
        "workspace_id": device_context.workspace_id,
        "device_id": device_context.device_id,
        "event_id": preflight.event_id,
        "idempotency_key": preflight.idempotency_key.value,
        "operation_kind": SmallFileOperation.CREATE.value,
        "declared_sha256": preflight.sha256.hexadecimal,
        "declared_size_bytes": preflight.size_bytes,
        "declared_media_type": preflight.media_type.value,
        "policy_revision_number": _POLICY_REVISION_NUMBER,
        "reserved_source_id": uuid4(),
        "update_source_id": None,
        "update_base_version_id": None,
        "normalized_locator": locator.value,
        "locator_fingerprint": compute_locator_fingerprint(locator),
        "state": state,
        "safe_error_code": safe_error_code,
        "result_kind": None,
        "result_source_id": None,
        "result_source_version_id": None,
        "result_content_version": None,
        "result_committed_at": None,
        "expires_at": expires_at,
    }
    return row, preflight, device_context


class _ReclaimScriptedConnection:
    """Connection double serving one identity row across reclaim sequences.

    Serves the same single-row view for the identity and token lookups the
    reservation and terminalization paths perform, counts every guarded
    write, and folds each rotation back onto the row view so a scripted
    sequence observes the state it previously wrote.
    """

    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row
        self.rotation_params: dict[str, Any] | None = None
        self.failure_write_count = 0
        self.update_count = 0

    def begin(self) -> _ScriptedBegin:
        return _ScriptedBegin()

    async def execute(self, statement: Any) -> _ScriptedResult:
        visit_name = statement.__visit_name__
        if visit_name == "select":
            # Both the identity lookup and the token lookup resolve to this row.
            return _ScriptedResult(mapping=self._row)
        if visit_name == "update":
            self.update_count += 1
            params = statement.compile(dialect=postgresql.dialect()).params
            if params["state"] == STATE_FAILED:
                self.failure_write_count += 1
            else:
                self.rotation_params = params
                self._row["operation_token_hash"] = params["operation_token_hash"]
                self._row["expires_at"] = params["expires_at"]
                self._row["policy_revision_number"] = params["policy_revision_number"]
            self._row["state"] = params["state"]
            if "safe_error_code" in params:
                self._row["safe_error_code"] = params["safe_error_code"]
            return _ScriptedResult(rowcount=1)
        if visit_name in {"text", "textclause"}:
            return _ScriptedResult()
        raise AssertionError(f"unexpected statement kind: {visit_name}")


def _reclaim_store(
    connection: _ReclaimScriptedConnection, *, clock: Callable[[], datetime]
) -> PostgresqlSmallFileUploadOperationStore:
    return PostgresqlSmallFileUploadOperationStore(
        cast(Any, _ReceiveScriptedEngine(connection)), clock=clock
    )


@pytest.mark.asyncio
async def test_re_preflight_reclaims_a_terminal_failed_row_with_a_fresh_token() -> None:
    """A failed identity row re-reserves exactly like the expired-pending branch.

    The typed terminal rejection parks the durable row at ``failed``; the
    plugin's bounded retry re-preflights the same identity, and the durable
    adapter must answer the fresh claim the offline composition pins — a new
    opaque token, the state reset to pending with the stale failure token
    cleared, the changed policy revision and the extended deadline — instead
    of the closed state-invalid rejection that would park the journal
    forever.
    """

    now = datetime.now(UTC)
    row, preflight, device_context = _reclaim_row_and_preflight(
        state=STATE_FAILED,
        safe_error_code=_FAILED_SAFE_ERROR_CODE,
        expires_at=now - timedelta(seconds=30),
    )
    changed_revision = _POLICY_REVISION_NUMBER + 3
    connection = _ReclaimScriptedConnection(row)

    claim = await _reclaim_store(connection, clock=lambda: now).reserve_operation(
        preflight,
        device_context,
        _policy_binding(device_context, changed_revision),
        cast(Any, object()),
    )

    assert claim.operation_token.value != _SUPERSEDED_RAW_TOKEN.value
    assert connection.rotation_params is not None
    rotation = connection.rotation_params
    assert rotation["state"] == STATE_PENDING
    assert rotation["safe_error_code"] is None
    assert rotation["policy_revision_number"] == changed_revision
    assert rotation["expires_at"] == compute_upload_operation_expiry(now)
    assert rotation["operation_token_hash"] == upload_operation_token_hash(claim.operation_token)
    assert claim.reserved_source_id == row["reserved_source_id"]
    assert claim.expires_at == compute_upload_operation_expiry(now)
    assert row["state"] == STATE_PENDING
    assert row["safe_error_code"] is None


@pytest.mark.asyncio
async def test_re_preflight_still_refuses_a_committed_row() -> None:
    """A committed identity row keeps its closed state-invalid rejection.

    The failed-row reclaim must not widen into a committed-row reclaim: the
    frozen terminal result of a committed row stays reachable only through
    ``resolve_terminal_result``, so a same-identity re-preflight keeps
    failing closed with no write.
    """

    committed_at = datetime.now(UTC) - timedelta(minutes=2)
    row, preflight, device_context = _reclaim_row_and_preflight(
        state="committed", safe_error_code=None, expires_at=committed_at
    )
    row["result_kind"] = "committed"
    row["result_source_id"] = row["reserved_source_id"]
    row["result_source_version_id"] = uuid4()
    row["result_content_version"] = 1
    row["result_committed_at"] = committed_at
    connection = _ReclaimScriptedConnection(row)

    with pytest.raises(SmallFileSyncError) as rejected:
        await _reclaim_store(connection, clock=lambda: datetime.now(UTC)).reserve_operation(
            preflight,
            device_context,
            _policy_binding(device_context, _POLICY_REVISION_NUMBER),
            cast(Any, object()),
        )

    assert rejected.value.error_code is ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID
    assert connection.update_count == 0
    assert connection.rotation_params is None


@pytest.mark.asyncio
async def test_failed_row_terminalization_replay_stays_idempotent_before_reclaim() -> None:
    """The Task 1 idempotent pair holds until the fresh claim rotates the row.

    Replaying the identical bound/code pair on the failed row writes nothing
    (the terminalization idempotent pair), a live failed row keeps its
    remaining deadline, and only the subsequent same-identity preflight
    mints the fresh claim that retires the superseded token.
    """

    row, preflight, device_context = _reclaim_row_and_preflight(
        state=STATE_FAILED,
        safe_error_code=_FAILED_SAFE_ERROR_CODE,
        expires_at=datetime.now(UTC) + timedelta(seconds=UPLOAD_OPERATION_EXPIRY_SECONDS),
    )
    locator = preflight.normalized_locator
    assert locator is not None
    bound = SmallFileBoundOperation(
        operation_id=row["operation_id"],
        operation_token=_SUPERSEDED_RAW_TOKEN,
        workspace_id=device_context.workspace_id,
        device_id=device_context.device_id,
        event_id=preflight.event_id,
        idempotency_key=preflight.idempotency_key,
        operation=SmallFileOperation.CREATE,
        declared_sha256=preflight.sha256,
        declared_size_bytes=preflight.size_bytes,
        declared_media_type=preflight.media_type,
        policy_revision_number=_POLICY_REVISION_NUMBER,
        reserved_source_id=row["reserved_source_id"],
        update_source_id=None,
        update_base_version_id=None,
        normalized_locator=locator,
        locator_fingerprint=compute_locator_fingerprint(locator),
        expires_at=row["expires_at"],
        terminal_result=None,
    )
    connection = _ReclaimScriptedConnection(row)
    store = _reclaim_store(connection, clock=lambda: datetime.now(UTC))

    await store.record_bound_terminal_failure(
        bound, ErrorCode.SOURCE_LOCATOR_CONFLICT, cast(Any, object())
    )
    await store.record_bound_terminal_failure(
        bound, ErrorCode.SOURCE_LOCATOR_CONFLICT, cast(Any, object())
    )
    assert connection.failure_write_count == 0
    assert connection.update_count == 0

    claim = await store.reserve_operation(
        preflight,
        device_context,
        _policy_binding(device_context, _POLICY_REVISION_NUMBER),
        cast(Any, object()),
    )

    assert claim.operation_token.value != _SUPERSEDED_RAW_TOKEN.value
    assert connection.rotation_params is not None
    assert connection.rotation_params["safe_error_code"] is None
    # A live failed row keeps its remaining deadline, exactly like a live
    # pending row; only an expired one extends.
    assert claim.expires_at == row["expires_at"]
