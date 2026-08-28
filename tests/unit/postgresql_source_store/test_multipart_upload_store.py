"""Multipart session store statements, fencing guards and closed error seams.

These tests pin the pure pieces of the PostgreSQL multipart session store
without a database: the opaque public session-ID minting against the domain
grammar, the privacy redaction of the store's hydrated view, the
schema-qualified parameter-bound statement shapes (row-locked session and
operation lookups, the guarded compare-and-set completion claim, terminal
and cleanup writes, the expiry sweep and the skip-locked bounded cleanup
claim), the finite lease/backoff constants, the typed database-failure
mapping into the closed multipart registry and the retry policy that only
contention may retry. The durable transaction, race and query-plan behavior
is integration territory (disposable CI stack).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import sqlalchemy as sa
import sqlalchemy.exc as sa_exc
from sqlalchemy.dialects import postgresql

from personal_os.error_contracts.codes import ErrorCode
from personal_os.multipart_upload.contracts import (
    MultipartSessionState,
    MultipartUploadSessionId,
)
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.ports import (
    MultipartProviderPartETag,
    MultipartProviderUploadId,
    MultipartSessionClaim,
    MultipartSessionRecord,
    SealedMultipartOperationToken,
)
from personal_os.object_storage import CanonicalMediaType, ContentDigest
from personal_os.small_file_sync.contracts import (
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
    UploadOperationToken,
)
from postgresql_source_store.multipart_upload_store import (
    MULTIPART_CLEANUP_RETRY_BASE_SECONDS,
    MULTIPART_CLEANUP_RETRY_MAXIMUM_SECONDS,
    MULTIPART_COMPLETION_LEASE_SECONDS,
    MultipartDatabaseRetryPolicy,
    PostgresqlMultipartUploadStore,
    cleanup_claim_select_statement,
    cleanup_failure_update_statement,
    cleanup_success_update_statement,
    completion_claim_transition_statement,
    compute_cleanup_next_retry,
    compute_completion_lease_expiry,
    expiry_sweep_select_statement,
    map_multipart_database_failure,
    mint_multipart_session_id,
    multipart_operation_select_statement,
    multipart_sealed_token_material,
    multipart_session_insert_statement,
    multipart_session_select_statement,
    operation_row_by_id_select_statement,
    operation_token_seal_update_statement,
    provider_identity_update_statement,
    require_terminal_failure_state,
    terminal_failure_update_statement,
    terminal_result_update_statement,
)

_SENTINEL_STATEMENT = "SELECT do-not-emit-sql FROM knowledge.multipart_uploads"
_SENTINEL_DRIVER_TEXT = "do-not-emit-driver-text"
_STORE_MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "postgresql-source-store"
    / "src"
    / "postgresql_source_store"
    / "multipart_upload_store.py"
)
_DIGEST = ContentDigest.parse("a" * 64)
_MEDIA_TYPE = CanonicalMediaType.parse("text/markdown")
_SESSION_ID = MultipartUploadSessionId("s" * 43)
_STAGING_KEY = "staging/exact/private-key"
_PROVIDER_UPLOAD_ID = MultipartProviderUploadId("provider-upload-identity")
_ETAG = MultipartProviderPartETag("provider-observed-etag")
_NOW = datetime(2026, 8, 28, 10, 0, 0, tzinfo=UTC)
_RAW_OPERATION_TOKEN = UploadOperationToken("t" * 43)
_SEALED_CIPHERTEXT = "c2VhbGVkLWNpcGhlcnRleHQtZW5jcnlwdGVkLXNlbnRpbmVs"
_SEALED_NONCE = "c2VhbGVkLW5vbmNlLXNlbnRpbmVs"


def _sealed_token() -> SealedMultipartOperationToken:
    return SealedMultipartOperationToken(
        key_id="auth-key-v1",
        nonce=_SEALED_NONCE,
        ciphertext=_SEALED_CIPHERTEXT,
    )


def _multipart_row(
    state: MultipartSessionState,
    *,
    sealed_ciphertext: str | None = _SEALED_CIPHERTEXT,
    sealed_nonce: str | None = _SEALED_NONCE,
    sealed_key_id: str | None = "auth-key-v1",
) -> Any:
    """Build one hydrated session row view for the pure row-shape tests."""

    from postgresql_source_store.multipart_upload_store import MultipartSessionRow

    return MultipartSessionRow(
        multipart_upload_id=uuid4(),
        session_id_value=_SESSION_ID.value,
        workspace_id=uuid4(),
        device_id=uuid4(),
        operation_id=uuid4(),
        declared_sha256=_DIGEST.hexadecimal,
        declared_size_bytes=24 * 1024 * 1024,
        declared_media_type=_MEDIA_TYPE.value,
        base_version_id=None,
        policy_revision_number=4,
        part_size_bytes=8 * 1024 * 1024,
        part_count=3,
        staging_key=_STAGING_KEY,
        provider_upload_id_value=_PROVIDER_UPLOAD_ID.value,
        sealed_ciphertext=sealed_ciphertext,
        sealed_nonce=sealed_nonce,
        sealed_key_id=sealed_key_id,
        state=state,
        claim_token=None,
        claim_expires_at=None,
        result_kind=None,
        result_source_id=None,
        result_source_version_id=None,
        result_content_version=None,
        result_committed_at=None,
        cleanup_state="none",
        cleanup_attempt_count=0,
        cleanup_next_retry_at=None,
        cleanup_reason_code=None,
        expires_at=_NOW + timedelta(hours=24),
    )


class _DriverFailure(Exception):
    """Fake driver exception carrying a SQLSTATE and sentinel driver text."""

    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(_SENTINEL_DRIVER_TEXT)
        self.sqlstate = sqlstate


def _contention_failure() -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("40P01"))


def _unclassified_failure() -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("23505"))


def _terminal_result() -> SmallFileTerminalResult:
    return SmallFileTerminalResult(
        result_kind=SmallFileTerminalResultKind.COMMITTED,
        source_id=uuid4(),
        source_version_id=uuid4(),
        content_version=1,
        committed_at=_NOW,
    )


def _session_record(state: MultipartSessionState) -> MultipartSessionRecord:
    return MultipartSessionRecord(
        session_id=_SESSION_ID,
        state=state,
        part_size_bytes=8 * 1024 * 1024,
        part_count=3,
        total_size_bytes=20 * 1024 * 1024,
        expires_at=_NOW + timedelta(hours=24),
        staging_key=_STAGING_KEY,
        provider_upload_id=_PROVIDER_UPLOAD_ID,
        completed_part_numbers=frozenset(),
        terminal_result=None,
    )


def _compiled(statement: sa.ClauseElement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
    )


class TestSessionIdMinting:
    def test_minted_session_id_matches_public_grammar(self) -> None:
        minted = mint_multipart_session_id()
        allowed_characters = set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )
        assert 32 <= len(minted.value) <= 128
        assert all(char in allowed_characters for char in minted.value)

    def test_minted_session_ids_are_unique(self) -> None:
        assert len({mint_multipart_session_id().value for _ in range(64)}) == 64


class TestPrivacyRedaction:
    def test_session_record_repr_redacts_private_provider_identity(self) -> None:
        rendered = repr(_session_record(MultipartSessionState.UPLOADING))
        assert _STAGING_KEY not in rendered
        assert _PROVIDER_UPLOAD_ID.value not in rendered
        assert "<redacted>" in rendered

    def test_cleanup_and_completion_claims_inherit_redaction(self) -> None:
        claim = MultipartSessionClaim(
            session=_session_record(MultipartSessionState.COMPLETING),
            claim_token=uuid4(),
            claim_expires_at=_NOW + timedelta(seconds=60),
        )
        rendered = repr(claim)
        assert _STAGING_KEY not in rendered
        assert _PROVIDER_UPLOAD_ID.value not in rendered

    def test_sealed_token_material_requires_the_full_trio(self) -> None:
        row = _multipart_row(MultipartSessionState.UPLOADING)
        sealed = multipart_sealed_token_material(row)
        assert sealed.key_id == "auth-key-v1"
        assert sealed.ciphertext == _SEALED_CIPHERTEXT
        assert sealed.nonce == _SEALED_NONCE

    def test_sealed_token_material_fails_closed_on_a_partial_seal(self) -> None:
        row = _multipart_row(MultipartSessionState.UPLOADING, sealed_nonce=None)
        with pytest.raises(MultipartUploadError) as info:
            multipart_sealed_token_material(row)
        assert info.value.error_code is ErrorCode.MULTIPART_SESSION_STATE_INVALID

    def test_sealed_token_material_fails_closed_on_an_absent_seal(self) -> None:
        row = _multipart_row(
            MultipartSessionState.UPLOADING,
            sealed_ciphertext=None,
            sealed_nonce=None,
            sealed_key_id=None,
        )
        with pytest.raises(MultipartUploadError) as info:
            multipart_sealed_token_material(row)
        assert info.value.error_code is ErrorCode.MULTIPART_SESSION_STATE_INVALID


class TestStatementShapes:
    def test_operation_lookup_takes_row_lock_for_session_mutation(self) -> None:
        operation_id = uuid4()
        compiled = _compiled(multipart_operation_select_statement(operation_id))
        assert "FOR UPDATE" in compiled
        assert "knowledge.multipart_uploads" in compiled
        assert str(operation_id) not in compiled

    def test_operation_lookup_can_run_lock_free(self) -> None:
        compiled = _compiled(
            multipart_operation_select_statement(uuid4(), for_update=False)
        )
        assert "FOR UPDATE" not in compiled

    def test_session_lookup_takes_row_lock(self) -> None:
        compiled = _compiled(multipart_session_select_statement(_SESSION_ID))
        assert "FOR UPDATE" in compiled
        assert "knowledge.multipart_uploads" in compiled
        assert _SESSION_ID.value not in compiled

    def test_reserve_insert_is_parameter_bound_and_defers_provider_identity(self) -> None:
        statement = multipart_session_insert_statement(
            multipart_upload_id=uuid4(),
            session_id_value=_SESSION_ID.value,
            workspace_id=uuid4(),
            device_id=uuid4(),
            operation_id=uuid4(),
            declared_sha256=_DIGEST.hexadecimal,
            declared_size_bytes=20 * 1024 * 1024,
            declared_media_type=_MEDIA_TYPE.value,
            base_version_id=None,
            policy_revision_number=4,
            part_size_bytes=8 * 1024 * 1024,
            part_count=3,
            expires_at=_NOW + timedelta(hours=24),
            sealed_token=_sealed_token(),
        )
        compiled = _compiled(statement)
        # The reservation happens before the provider create: the row opens
        # with NULL private identity and no session value is SQL text.
        assert _SESSION_ID.value not in compiled
        assert "knowledge.multipart_uploads" in compiled
        # The sealed raw-token preimage is parameter-bound sealed text, and
        # neither the raw token nor its sealed material is SQL text.
        assert _RAW_OPERATION_TOKEN.value not in compiled
        assert _SEALED_CIPHERTEXT not in compiled
        assert _SEALED_NONCE not in compiled
        assert "operation_token_ciphertext" in compiled
        assert "operation_token_nonce" in compiled
        assert "operation_token_key_id" in compiled

    def test_reserve_insert_accepts_an_absent_seal(self) -> None:
        statement = multipart_session_insert_statement(
            multipart_upload_id=uuid4(),
            session_id_value=_SESSION_ID.value,
            workspace_id=uuid4(),
            device_id=uuid4(),
            operation_id=uuid4(),
            declared_sha256=_DIGEST.hexadecimal,
            declared_size_bytes=20 * 1024 * 1024,
            declared_media_type=_MEDIA_TYPE.value,
            base_version_id=None,
            policy_revision_number=4,
            part_size_bytes=8 * 1024 * 1024,
            part_count=3,
            expires_at=_NOW + timedelta(hours=24),
            sealed_token=None,
        )
        compiled = _compiled(statement)
        # A composition without a codec reserves with no seal: the columns
        # bind NULL and the durable evidence read fails closed later.
        assert _SESSION_ID.value not in compiled
        assert "knowledge.multipart_uploads" in compiled

    def test_operation_token_seal_refresh_is_parameter_bound(self) -> None:
        statement = operation_token_seal_update_statement(
            session_id_value=_SESSION_ID.value,
            sealed_token=_sealed_token(),
        )
        compiled = _compiled(statement)
        assert _SESSION_ID.value not in compiled
        assert _SEALED_CIPHERTEXT not in compiled
        assert _SEALED_NONCE not in compiled
        assert "knowledge.multipart_uploads" in compiled
        # The refresh addresses exactly one session row by its opaque ID.
        assert "session_id" in compiled

    def test_operation_row_by_id_lookup_is_lock_free_and_parameter_bound(self) -> None:
        operation_id = uuid4()
        compiled = _compiled(operation_row_by_id_select_statement(operation_id))
        assert str(operation_id) not in compiled
        assert "FOR UPDATE" not in compiled
        assert "knowledge.small_file_upload_operations" in compiled

    def test_sealed_token_value_renders_redacted(self) -> None:
        sealed = _sealed_token()
        rendered = repr(sealed)
        assert _SEALED_CIPHERTEXT not in rendered
        assert _SEALED_NONCE not in rendered
        assert "ciphertext" not in rendered

    def test_provider_identity_write_is_guarded_compare_and_set(self) -> None:
        statement = provider_identity_update_statement(
            session_id_value=_SESSION_ID.value,
            staging_key=_STAGING_KEY,
            provider_upload_id_value=_PROVIDER_UPLOAD_ID.value,
        )
        compiled = _compiled(statement)
        # Private provider identity is parameter-bound, never SQL text.
        assert _STAGING_KEY not in compiled
        assert _PROVIDER_UPLOAD_ID.value not in compiled
        # The guard admits exactly the identity-absent, claim-free
        # pre-completion shape.
        assert "staging_key IS NULL" in compiled
        assert "provider_upload_id IS NULL" in compiled
        assert "claim_token IS NULL" in compiled

    def test_completion_claim_transition_is_guarded_compare_and_set(self) -> None:
        claim_token = uuid4()
        claim_expiry = _NOW + timedelta(seconds=600)
        statement = completion_claim_transition_statement(
            session_id_value=_SESSION_ID.value,
            claim_token=claim_token,
            claim_expires_at=claim_expiry,
        )
        compiled = _compiled(statement)
        assert str(claim_token) not in compiled
        assert "knowledge.multipart_uploads" in compiled

    def test_terminal_result_update_guards_claim_token_and_state(self) -> None:
        statement = terminal_result_update_statement(
            session_id_value=_SESSION_ID.value,
            claim_token=uuid4(),
            result=_terminal_result(),
        )
        compiled = _compiled(statement)
        assert "claim_token" in compiled
        assert "knowledge.multipart_uploads" in compiled

    def test_terminal_failure_update_clears_lease_and_schedules_cleanup(self) -> None:
        statement = terminal_failure_update_statement(
            session_id_value=_SESSION_ID.value,
            claim_token=uuid4(),
            failure_state=MultipartSessionState.INTEGRITY_FAILED,
            now=_NOW,
        )
        compiled = _compiled(statement)
        assert "cleanup_state" in compiled
        assert "cleanup_next_retry_at" in compiled
        assert "knowledge.multipart_uploads" in compiled

    def test_expiry_sweep_select_is_bounded_ordered_and_skip_locked(self) -> None:
        statement = expiry_sweep_select_statement(now=_NOW, batch_limit=25)
        compiled = _compiled(statement)
        assert "FOR UPDATE SKIP LOCKED" in compiled
        assert "LIMIT" in compiled
        assert "ORDER BY" in compiled

    def test_cleanup_claim_select_is_bounded_ordered_and_skip_locked(self) -> None:
        statement = cleanup_claim_select_statement(now=_NOW, batch_limit=25)
        compiled = _compiled(statement)
        assert "FOR UPDATE SKIP LOCKED" in compiled
        assert "LIMIT" in compiled
        assert "ORDER BY" in compiled

    def test_cleanup_success_update_clears_obligation_and_lease(self) -> None:
        statement = cleanup_success_update_statement(
            session_id_value=_SESSION_ID.value,
            claim_token=uuid4(),
            now=_NOW,
        )
        compiled = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "cleaned" in compiled
        assert "succeeded" in compiled
        assert "cleanup_next_retry_at" in compiled

    def test_cleanup_failure_update_persists_reason_and_next_retry(self) -> None:
        statement = cleanup_failure_update_statement(
            session_id_value=_SESSION_ID.value,
            claim_token=uuid4(),
            failure_reason=ErrorCode.MULTIPART_CLEANUP_FAILED,
            attempt_count=2,
            next_retry_at=_NOW + timedelta(minutes=4),
            now=_NOW,
        )
        compiled = _compiled(statement)
        assert "cleanup_attempt_count" in compiled
        assert "knowledge.multipart_uploads" in compiled


class TestLeaseAndBackoffConstants:
    def test_completion_lease_is_finite_and_positive(self) -> None:
        assert MULTIPART_COMPLETION_LEASE_SECONDS > 0

    def test_completion_lease_expiry_requires_aware_utc_now(self) -> None:
        expiry = compute_completion_lease_expiry(_NOW)
        assert expiry == _NOW + timedelta(seconds=MULTIPART_COMPLETION_LEASE_SECONDS)
        with pytest.raises(ValueError):
            compute_completion_lease_expiry(_NOW.replace(tzinfo=None))

    def test_cleanup_backoff_is_bounded_and_monotonic(self) -> None:
        base = timedelta(seconds=MULTIPART_CLEANUP_RETRY_BASE_SECONDS)
        maximum = timedelta(seconds=MULTIPART_CLEANUP_RETRY_MAXIMUM_SECONDS)
        first = compute_cleanup_next_retry(_NOW, attempt_count=1)
        assert first == _NOW + base
        for attempt_count in range(1, 40):
            deadline = compute_cleanup_next_retry(_NOW, attempt_count=attempt_count)
            assert deadline - _NOW <= maximum
        # Strict growth holds while the exponential stays below the cap.
        assert compute_cleanup_next_retry(_NOW, attempt_count=4) > compute_cleanup_next_retry(
            _NOW, attempt_count=3
        )
        # Beyond the cap every attempt waits exactly the ceiling.
        assert compute_cleanup_next_retry(_NOW, attempt_count=30) == _NOW + maximum


class TestClosedErrorBoundary:
    def test_database_failure_maps_to_typed_dependency_error(self) -> None:
        error = map_multipart_database_failure(_unclassified_failure())
        assert isinstance(error, MultipartUploadError)
        assert error.error_code is ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE
        assert error.is_retryable is True
        # The registry message and code are the only text rendered.
        assert _SENTINEL_DRIVER_TEXT not in str(error)
        assert _SENTINEL_STATEMENT not in str(error)

    def test_non_database_failure_maps_to_typed_dependency_error(self) -> None:
        error = map_multipart_database_failure(RuntimeError(_SENTINEL_DRIVER_TEXT))
        assert isinstance(error, MultipartUploadError)
        assert error.error_code is ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE
        assert _SENTINEL_DRIVER_TEXT not in str(error)

    @pytest.mark.asyncio
    async def test_retry_policy_passes_typed_errors_through(self) -> None:
        policy = MultipartDatabaseRetryPolicy()
        typed_error = MultipartUploadError(ErrorCode.MULTIPART_SESSION_STATE_INVALID)

        async def operation(_attempt: int) -> None:
            raise typed_error

        with pytest.raises(MultipartUploadError) as raised:
            await policy.run(operation, sleep=_fail_sleep)
        assert raised.value.error_code is ErrorCode.MULTIPART_SESSION_STATE_INVALID

    @pytest.mark.asyncio
    async def test_retry_policy_retries_only_contention(self) -> None:
        policy = MultipartDatabaseRetryPolicy(maximum_attempts=3)
        attempts: list[int] = []

        async def contention_then_success(_attempt: int) -> str:
            attempts.append(_attempt)
            if len(attempts) < 3:
                raise _contention_failure()
            return "resolved"

        assert await policy.run(contention_then_success, sleep=_record_sleep) == "resolved"
        assert attempts == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_retry_policy_maps_exhausted_contention_to_typed_error(self) -> None:
        policy = MultipartDatabaseRetryPolicy(maximum_attempts=2)

        async def always_contended(_attempt: int) -> None:
            raise _contention_failure()

        with pytest.raises(MultipartUploadError) as raised:
            await policy.run(always_contended, sleep=_record_sleep)
        assert raised.value.error_code is ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_retry_policy_maps_unclassified_failure_immediately(self) -> None:
        policy = MultipartDatabaseRetryPolicy(maximum_attempts=3)
        attempts: list[int] = []

        async def always_unclassified(_attempt: int) -> None:
            attempts.append(_attempt)
            raise _unclassified_failure()

        with pytest.raises(MultipartUploadError) as raised:
            await policy.run(always_unclassified, sleep=_fail_sleep)
        assert raised.value.error_code is ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE
        assert attempts == [1]


class TestTerminalOutcomeValidation:
    def test_failure_state_outside_closed_obligations_is_rejected(self) -> None:
        for state in (
            MultipartSessionState.COMMITTED,
            MultipartSessionState.CLEANED,
            MultipartSessionState.EXPIRED,
            MultipartSessionState.UPLOADING,
        ):
            with pytest.raises(MultipartUploadError) as raised:
                require_terminal_failure_state(state)
            assert raised.value.error_code is ErrorCode.MULTIPART_SESSION_STATE_INVALID

    def test_failure_state_inside_closed_obligations_is_accepted(self) -> None:
        for state in (
            MultipartSessionState.CANCELLING,
            MultipartSessionState.INTEGRITY_FAILED,
            MultipartSessionState.POLICY_DENIED,
        ):
            assert require_terminal_failure_state(state) is None

    @pytest.mark.asyncio
    async def test_record_terminal_result_requires_exactly_one_outcome(self) -> None:
        store = PostgresqlMultipartUploadStore(
            cast(Any, None), clock=lambda: _NOW
        )
        claim = MultipartSessionClaim(
            session=_session_record(MultipartSessionState.COMPLETING),
            claim_token=uuid4(),
            claim_expires_at=_NOW + timedelta(seconds=60),
        )
        with pytest.raises(ValueError):
            await store.record_terminal_result(
                claim=claim,
                result=_terminal_result(),
                failure_state=MultipartSessionState.INTEGRITY_FAILED,
                diagnostic_context=None,  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError):
            await store.record_terminal_result(
                claim=claim,
                result=None,
                failure_state=None,
                diagnostic_context=None,  # type: ignore[arg-type]
            )


class TestProviderBoundary:
    def test_store_module_imports_no_provider_sdk_or_logging(self) -> None:
        source = _STORE_MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "aiobotocore",
            "boto3",
            "r2_object_storage",
            "import logging",
            "from logging",
            "structlog",
        ):
            assert forbidden not in source, (
                f"the session store must stay free of provider/log seams: {forbidden}"
            )

    def test_store_module_declares_no_object_storage_import(self) -> None:
        source = _STORE_MODULE_PATH.read_text(encoding="utf-8")
        assert "personal_os.object_storage" not in source


async def _fail_sleep(delay: float) -> None:
    raise AssertionError("the retry policy must not sleep on this path")


async def _record_sleep(delay: float) -> None:
    return None


__all__ = [
    "TestClosedErrorBoundary",
    "TestLeaseAndBackoffConstants",
    "TestPrivacyRedaction",
    "TestProviderBoundary",
    "TestSessionIdMinting",
    "TestStatementShapes",
    "TestTerminalOutcomeValidation",
]
