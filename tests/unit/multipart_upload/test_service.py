"""Multipart upload service orchestration over faithful in-memory fakes.

Pins the exact ordered flows of the Child 7 spec: the persist-before-create
session reservation with its fenced provider-identity write (6.1), part-URL
issuance behind ownership/state/policy/range rechecks (6.2), status
reconciliation of provider-observed parts (6.1), the serialized completion
chain — rechecks, ListParts proof, CompleteMultipartUpload, the bounded full
verification spool, exactly one publication, the frozen terminal write, the
inline exact staging delete (6.3) — cancellation, and the exact cleanup
execution over only the persisted private identities (6.4). Every failure
path asserts both the closed typed error and the durable cleanup obligation,
and the privacy assertions prove no staging key, URL, digest or provider
identity ever enters a typed error.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from tests.unit.multipart_upload.fakes import (
    OBJECT_STORE_STORE_STREAM,
    PROVIDER_COMPLETE_UPLOAD,
    PROVIDER_CREATE_UPLOAD,
    PROVIDER_DELETE_STAGING,
    PROVIDER_LIST_PARTS,
    PUBLISH_UPDATE,
    SESSION_CLAIM_COMPLETION,
    SESSION_RECORD_CLEANUP,
    SESSION_RECORD_TERMINAL,
    SESSION_RESERVE,
    STAGING_OPEN_STREAM,
    DenyingSmallFilePolicyGuard,
    build_current_reference,
    build_device_context,
    build_diagnostic_context,
    build_multipart_service_harness,
    dependency_outage,
    stale_base_conflict,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.enforcement import AllowedPolicyRevisionBinding
from personal_os.multipart_upload.contracts import MultipartSessionState, MultipartUploadSessionId
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.object_storage import ContentDigest
from personal_os.small_file_sync.contracts import SmallFileTerminalResultKind
from personal_os.sources.errors import SourcePublicationError

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def harness():  # type: ignore[no-untyped-def]
    """One fully prepared harness: a created session with every part uploaded."""

    built = build_multipart_service_harness()
    await built.create_ready_session()
    return built


class TestCreateOrResume:
    async def test_session_row_persists_before_the_provider_create_call(self, harness) -> None:
        assert harness.ledger.first_index(SESSION_RESERVE) < harness.ledger.first_index(
            PROVIDER_CREATE_UPLOAD
        )
        assert harness.row().has_provider_identity()
        assert harness.row().state is MultipartSessionState.CREATED

    async def test_exact_replay_reuses_one_session_without_new_provider_work(self, harness) -> None:
        first_id = harness.session_id

        replay = await harness.service.create_or_resume(
            preflight=harness.preflight,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        assert replay.session_id == first_id
        assert harness.ledger.count(PROVIDER_CREATE_UPLOAD) == 1
        assert len(harness.session_store.rows) == 1

    async def test_divergent_provider_identity_aborts_the_callers_fresh_upload(self) -> None:
        built = build_multipart_service_harness()
        built.session_store.divergent_identity_injection = f"staging/multipart/{'k' * 40}"

        with pytest.raises(MultipartUploadError, match="multipart_provider_state_invalid"):
            await built.service.create_or_resume(
                preflight=built.preflight,
                device_context=built.device,
                diagnostic_context=build_diagnostic_context(),
            )

        # The reserved row owns the session identity the fresh staging key
        # derived from; the harness field is only synced by the ready flow.
        built.session_id = MultipartUploadSessionId(built.session_store.rows[0].session_id_value)
        assert len(built.staging_provider.aborted) == 1
        assert built.staging_provider.aborted[0][0] == built.staging_key()

    async def test_policy_denial_creates_no_session_and_no_provider_work(self) -> None:
        built = build_multipart_service_harness(denying_policy=True)

        with pytest.raises(MultipartUploadError, match="multipart_policy_denied"):
            await built.service.create_or_resume(
                preflight=built.preflight,
                device_context=built.device,
                diagnostic_context=build_diagnostic_context(),
            )

        assert built.session_store.rows == []
        assert built.ledger.count(PROVIDER_CREATE_UPLOAD) == 0

    async def test_stale_base_rejects_creation_with_the_existing_conflict_token(self) -> None:
        built = build_multipart_service_harness(stale_base=True)

        with pytest.raises(SourcePublicationError) as raised:
            await built.service.create_or_resume(
                preflight=built.preflight,
                device_context=built.device,
                diagnostic_context=build_diagnostic_context(),
            )

        assert raised.value.error_code is ErrorCode.SOURCE_VERSION_CONFLICT
        assert built.session_store.rows == []

    async def test_terminal_session_replay_fails_closed_without_new_provider_work(
        self, harness
    ) -> None:
        await harness.service.abort(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        with pytest.raises(MultipartUploadError, match="multipart_session_state_invalid"):
            await harness.service.create_or_resume(
                preflight=harness.preflight,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )

        # The replay resolves the durable row to observe its state, but no
        # second provider workload is ever minted for a terminal session.
        assert harness.ledger.count(PROVIDER_CREATE_UPLOAD) == 1
        assert len(harness.session_store.rows) == 1


class TestIssuePartUrl:
    async def test_issues_one_url_for_the_exact_part_range(self, harness) -> None:
        url = await harness.service.issue_part_url(
            session_id=harness.session_id,
            part_number=2,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        assert url.part_number == 2
        assert url.byte_range.size_bytes == 8 * 1024 * 1024
        assert url.byte_range.offset_bytes == 8 * 1024 * 1024

    async def test_foreign_device_cannot_obtain_a_part_url(self, harness) -> None:
        with pytest.raises(MultipartUploadError, match="multipart_session_not_found"):
            await harness.service.issue_part_url(
                session_id=harness.session_id,
                part_number=1,
                device_context=build_device_context(),
                diagnostic_context=harness.context,
            )

    async def test_out_of_range_part_number_fails_closed(self, harness) -> None:
        with pytest.raises(MultipartUploadError, match="multipart_part_invalid"):
            await harness.service.issue_part_url(
                session_id=harness.session_id,
                part_number=4,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )

    async def test_policy_denial_blocks_a_new_part_url(self) -> None:
        built = build_multipart_service_harness()
        await built.create_ready_session()
        built.service.policy_guard = DenyingSmallFilePolicyGuard()

        with pytest.raises(MultipartUploadError, match="multipart_policy_denied"):
            await built.service.issue_part_url(
                session_id=built.session_id,
                part_number=1,
                device_context=built.device,
                diagnostic_context=build_diagnostic_context(),
            )

    async def test_terminal_session_cannot_obtain_part_urls(self, harness) -> None:
        await harness.service.abort(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        with pytest.raises(MultipartUploadError, match="multipart_session_state_invalid"):
            await harness.service.issue_part_url(
                session_id=harness.session_id,
                part_number=1,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )


class TestStatus:
    async def test_status_reconciles_provider_observed_parts(self, harness) -> None:
        status = await harness.service.status(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        assert status.state is MultipartSessionState.UPLOADING
        assert status.completed_part_numbers == frozenset({1, 2, 3})
        assert status.part_count == 3

    async def test_status_reconciles_partial_progress_of_a_resuming_session(self) -> None:
        built = build_multipart_service_harness()
        await built.create_ready_session()
        staging_key = built.staging_key()
        upload_id = built.staging_provider.uploads[staging_key]
        del built.staging_provider.parts[upload_id][3]

        status = await built.service.status(
            session_id=built.session_id,
            device_context=built.device,
            diagnostic_context=build_diagnostic_context(),
        )

        assert status.state is MultipartSessionState.UPLOADING
        assert status.completed_part_numbers == frozenset({1, 2})

    async def test_status_returns_the_frozen_result_of_a_committed_session(self, harness) -> None:
        completed = await harness.service.complete(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )
        list_calls_after_completion = harness.ledger.count(PROVIDER_LIST_PARTS)

        status = await harness.service.status(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        assert status.state is MultipartSessionState.COMMITTED
        assert status.terminal_result == completed.terminal_result
        assert harness.ledger.count(PROVIDER_LIST_PARTS) == list_calls_after_completion

    async def test_status_during_active_completion_returns_persisted_state(self, harness) -> None:
        await harness.session_store.claim_completion(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        status = await harness.service.status(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        assert status.state is MultipartSessionState.COMPLETING
        assert harness.ledger.count(PROVIDER_LIST_PARTS) == 0

    async def test_expired_session_status_fails_closed(self, harness) -> None:
        harness.clock.advance(timedelta(hours=25))

        with pytest.raises(MultipartUploadError, match="multipart_session_expired"):
            await harness.service.status(
                session_id=harness.session_id,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )


class TestComplete:
    async def test_complete_full_verifies_then_publishes_once(self, harness) -> None:
        result = await harness.service.complete(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        assert result.terminal_result is not None
        assert result.terminal_result.result_kind is SmallFileTerminalResultKind.COMMITTED
        assert harness.publisher.calls == [PUBLISH_UPDATE]
        entries = harness.ledger.entries
        order = [
            SESSION_CLAIM_COMPLETION,
            PROVIDER_LIST_PARTS,
            PROVIDER_COMPLETE_UPLOAD,
            STAGING_OPEN_STREAM,
            OBJECT_STORE_STORE_STREAM,
            PUBLISH_UPDATE,
            SESSION_RECORD_TERMINAL,
            PROVIDER_DELETE_STAGING,
        ]
        indexes = [entries.index(entry) for entry in order]
        assert indexes == sorted(indexes)
        assert harness.row().state is MultipartSessionState.COMMITTED
        assert harness.staging_provider.objects == set()

    async def test_digest_mismatch_never_calls_publisher_and_schedules_cleanup(
        self, harness
    ) -> None:
        different_digest = ContentDigest.parse("a" * 64)
        harness.staging_reader.digest = different_digest

        with pytest.raises(MultipartUploadError, match="multipart_integrity_failed"):
            await harness.service.complete(
                session_id=harness.session_id,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )

        assert harness.publisher.calls == []
        assert harness.row().state is MultipartSessionState.INTEGRITY_FAILED
        assert harness.row().cleanup_state == "pending"

    async def test_complete_replay_returns_the_frozen_result_without_new_provider_work(
        self, harness
    ) -> None:
        first = await harness.service.complete(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        replay = await harness.service.complete(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        assert replay.terminal_result == first.terminal_result
        assert harness.publisher.calls == [PUBLISH_UPDATE]
        assert harness.ledger.count(PROVIDER_LIST_PARTS) == 1

    async def test_concurrent_completion_surfaces_the_closed_in_progress_token(
        self, harness
    ) -> None:
        await harness.session_store.claim_completion(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        with pytest.raises(MultipartUploadError, match="multipart_completion_in_progress"):
            await harness.service.complete(
                session_id=harness.session_id,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )

        assert harness.ledger.count(PROVIDER_LIST_PARTS) == 0

    async def test_policy_advance_lands_policy_denied_and_schedules_cleanup(self) -> None:
        built = build_multipart_service_harness()
        await built.create_ready_session()
        built.service.policy_guard = DenyingSmallFilePolicyGuard()

        with pytest.raises(MultipartUploadError, match="multipart_policy_denied"):
            await built.service.complete(
                session_id=built.session_id,
                device_context=built.device,
                diagnostic_context=build_diagnostic_context(),
            )

        assert built.publisher.calls == []
        assert built.ledger.count(PROVIDER_LIST_PARTS) == 0
        assert built.row().state is MultipartSessionState.POLICY_DENIED
        assert built.row().cleanup_state == "pending"

    async def test_stale_base_recheck_records_the_no_candidate_conflict_outcome(self) -> None:
        built = build_multipart_service_harness()
        await built.create_ready_session()
        # The base was current at creation and advanced before completion.
        built.current_sources.reference = build_current_reference(
            built.preflight, source_version_id=uuid4()
        )

        with pytest.raises(SourcePublicationError) as raised:
            await built.service.complete(
                session_id=built.session_id,
                device_context=built.device,
                diagnostic_context=build_diagnostic_context(),
            )

        assert raised.value.error_code is ErrorCode.SOURCE_VERSION_CONFLICT
        assert built.publisher.calls == []
        assert built.ledger.count(PROVIDER_COMPLETE_UPLOAD) == 0
        assert built.row().state is MultipartSessionState.CANCELLING
        assert built.row().cleanup_state == "pending"

    async def test_publication_stale_base_reports_the_no_candidate_outcome(self, harness) -> None:
        harness.publisher.error = stale_base_conflict()

        with pytest.raises(SourcePublicationError) as raised:
            await harness.service.complete(
                session_id=harness.session_id,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )

        assert raised.value.error_code is ErrorCode.SOURCE_VERSION_CONFLICT
        assert harness.publisher.calls == [PUBLISH_UPDATE]
        assert harness.row().state is MultipartSessionState.CANCELLING
        assert harness.row().cleanup_state == "pending"

    async def test_missing_part_fails_closed_before_any_complete(self, harness) -> None:
        staging_key = harness.staging_key()
        upload_id = harness.staging_provider.uploads[staging_key]
        del harness.staging_provider.parts[upload_id][3]

        with pytest.raises(MultipartUploadError, match="multipart_provider_state_invalid"):
            await harness.service.complete(
                session_id=harness.session_id,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )

        assert harness.ledger.count(PROVIDER_COMPLETE_UPLOAD) == 0
        assert harness.publisher.calls == []
        assert harness.row().state is MultipartSessionState.INTEGRITY_FAILED
        assert harness.row().cleanup_state == "pending"

    async def test_wrong_size_part_fails_closed_before_any_complete(self, harness) -> None:
        staging_key = harness.staging_key()
        upload_id = harness.staging_provider.uploads[staging_key]
        harness.staging_provider.parts[upload_id][3] = ("etag-3", 1024)

        with pytest.raises(MultipartUploadError, match="multipart_provider_state_invalid"):
            await harness.service.complete(
                session_id=harness.session_id,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )

        assert harness.ledger.count(PROVIDER_COMPLETE_UPLOAD) == 0
        assert harness.row().state is MultipartSessionState.INTEGRITY_FAILED

    async def test_lost_complete_response_fails_closed_through_the_retry(self, harness) -> None:
        harness.staging_provider.complete_error = dependency_outage()
        harness.staging_provider.complete_error_completes_anyway = True

        with pytest.raises(MultipartUploadError, match="multipart_dependency_unavailable"):
            await harness.service.complete(
                session_id=harness.session_id,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )
        assert harness.row().state is MultipartSessionState.COMPLETING

        harness.clock.advance(timedelta(seconds=601))
        with pytest.raises(MultipartUploadError, match="multipart_provider_state_invalid"):
            await harness.service.complete(
                session_id=harness.session_id,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )

        assert harness.publisher.calls == []
        assert harness.row().state is MultipartSessionState.INTEGRITY_FAILED
        assert harness.row().cleanup_state == "pending"

    async def test_base_exception_after_complete_persists_the_cleanup_obligation(
        self, harness
    ) -> None:
        harness.staging_reader.open_error = RuntimeError("staging read collapsed")

        with pytest.raises(RuntimeError, match="staging read collapsed"):
            await harness.service.complete(
                session_id=harness.session_id,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )

        assert harness.publisher.calls == []
        assert harness.row().state is MultipartSessionState.CANCELLING
        assert harness.row().cleanup_state == "pending"

    async def test_inline_staging_delete_failure_surfaces_its_closed_reason(self, harness) -> None:
        harness.staging_provider.delete_error = dependency_outage()

        result = await harness.service.complete(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        assert result.terminal_result is not None
        assert result.terminal_result.result_kind is SmallFileTerminalResultKind.COMMITTED
        reasons = {
            record.error_code.value
            for record in harness.metrics.rejection_diagnostics().recent_rejections
        }
        assert "multipart_dependency_unavailable" in reasons

    async def test_typed_errors_never_carry_private_values(self, harness) -> None:
        harness.staging_reader.digest = ContentDigest.parse("b" * 64)

        with pytest.raises(MultipartUploadError) as raised:
            await harness.service.complete(
                session_id=harness.session_id,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )

        error_text = str(raised.value)
        assert harness.staging_key() not in error_text
        assert harness.staging_reader.digest.hexadecimal not in error_text
        assert "https://" not in error_text
        assert (harness.row().provider_upload_id_value or "") not in error_text


class TestAbort:
    async def test_abort_terminalizes_cancellation_and_schedules_cleanup(self, harness) -> None:
        status = await harness.service.abort(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        assert status.state is MultipartSessionState.CANCELLING
        assert status.terminal_result is None
        assert harness.row().cleanup_state == "pending"

        with pytest.raises(MultipartUploadError, match="multipart_session_state_invalid"):
            await harness.service.complete(
                session_id=harness.session_id,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )

    async def test_abort_of_a_committed_session_fails_closed(self, harness) -> None:
        await harness.service.complete(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        with pytest.raises(MultipartUploadError, match="multipart_session_state_invalid"):
            await harness.service.abort(
                session_id=harness.session_id,
                device_context=harness.device,
                diagnostic_context=harness.context,
            )

    async def test_abort_is_idempotent_for_an_already_cancelled_session(self, harness) -> None:
        first = await harness.service.abort(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )
        terminal_writes = harness.ledger.count(SESSION_RECORD_TERMINAL)

        second = await harness.service.abort(
            session_id=harness.session_id,
            device_context=harness.device,
            diagnostic_context=harness.context,
        )

        assert second.state is MultipartSessionState.CANCELLING
        assert first.state is second.state
        assert harness.ledger.count(SESSION_RECORD_TERMINAL) == terminal_writes


class TestRunExactCleanup:
    async def test_aborts_and_deletes_only_the_exact_persisted_identities(self, harness) -> None:
        staging_key = harness.staging_key()
        upload_id = harness.row().provider_upload_id_value
        assert upload_id is not None
        harness.clock.advance(timedelta(hours=25))

        outcome = await harness.service.run_exact_cleanup(
            batch_limit=5, diagnostic_context=harness.context
        )

        assert outcome.cleaned_count == 1
        assert outcome.failed_count == 0
        assert harness.staging_provider.aborted == [(staging_key, upload_id)]
        assert harness.staging_provider.deleted == [staging_key]
        assert harness.row().state is MultipartSessionState.CLEANED
        assert harness.ledger.count(SESSION_RECORD_CLEANUP) == 1

    async def test_cleanup_failure_persists_the_closed_reason_and_next_retry(self, harness) -> None:
        harness.staging_provider.abort_error = dependency_outage()
        harness.clock.advance(timedelta(hours=25))

        outcome = await harness.service.run_exact_cleanup(
            batch_limit=5, diagnostic_context=harness.context
        )

        assert outcome.failed_count == 1
        assert outcome.cleaned_count == 0
        assert harness.session_store.cleanup_results == [
            (harness.row().session_id_value, False, "multipart_dependency_unavailable")
        ]
        assert harness.row().cleanup_state == "failed"
        assert harness.row().cleanup_next_retry_at is not None

    async def test_untyped_cleanup_failure_records_the_closed_cleanup_token(self, harness) -> None:
        harness.staging_provider.abort_error = RuntimeError("provider transport collapsed")
        harness.clock.advance(timedelta(hours=25))

        outcome = await harness.service.run_exact_cleanup(
            batch_limit=5, diagnostic_context=harness.context
        )

        assert outcome.failed_count == 1
        assert outcome.cleaned_count == 0
        assert harness.session_store.cleanup_results == [
            (harness.row().session_id_value, False, "multipart_cleanup_failed")
        ]
        assert harness.row().cleanup_state == "failed"
        reasons = {
            record.error_code.value
            for record in harness.metrics.rejection_diagnostics().recent_rejections
        }
        assert "multipart_cleanup_failed" in reasons

    async def test_identityless_expired_session_is_trivially_cleaned(self) -> None:
        built = build_multipart_service_harness()
        policy_binding = AllowedPolicyRevisionBinding(
            workspace_id=built.device.workspace_id, policy_revision_number=7
        )
        operation = await built.operation_store.reserve_operation(
            built.preflight, built.device, policy_binding, build_diagnostic_context()
        )
        reserved = await built.session_store.reserve_session(
            operation=operation,
            device_context=built.device,
            diagnostic_context=build_diagnostic_context(),
        )
        built.session_id = reserved.session_id
        built.clock.advance(timedelta(hours=25))

        outcome = await built.service.run_exact_cleanup(
            batch_limit=5, diagnostic_context=build_diagnostic_context()
        )

        assert outcome.cleaned_count == 1
        assert built.ledger.count(PROVIDER_DELETE_STAGING) == 0
