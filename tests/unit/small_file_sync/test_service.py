"""Small-file sync service orchestration: preflight, receive and publication.

Proves the normative flows of spec 10.1-10.3 against the REAL publication
service over recording fakes: server-side policy denial before any operation
store or object-store access, policy DENIALS on both the authorize and the
read boundary mapping to the same terminal ``excluded`` outcome with their
closed code on the rejection ring while policy SYSTEM failures propagate as
the typed 409/503 errors (G1), exact preflight replay of a
frozen terminal result, pending same-identity reservation with token rotation,
payload substitution rejection, create reservation without a source insert,
stale and missing update bases as durable conflicts, the frozen no-change
receipt, content-integrity failures that never publish, response-loss replay on
both paths, exactly one canonical publication under concurrent receives,
expiry and the server-owned size ceiling before any spool, and the closed
durable title/type derivation for creates. Assertions use only ledger
strings, closed enums, counts and value equality — never locator, digest,
token or payload sentinels.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Final
from uuid import uuid4

import pytest
from tests.unit.small_file_sync.fakes import (
    CURRENT_SOURCES_RESOLVE,
    OBJECT_STORE_RESOLVE,
    OBJECT_STORE_STORE_STREAM,
    PUBLICATION_COMMIT_CREATE,
    PUBLICATION_COMMIT_UPDATE,
    PUBLICATION_GUARD,
    PUBLICATION_RESOLVE_COMMITTED,
    STORE_RECORD_BOUND_TERMINAL,
    STORE_RECORD_BOUND_TERMINAL_FAILURE,
    STORE_RESERVE_OPERATION,
    STORE_RESOLVE_BOUND,
    STORE_RESOLVE_TERMINAL,
    SYNC_CONTENT_BYTES,
    SYNC_POLICY_GUARD,
    ProbedByteStream,
    ServiceHarness,
    build_create_preflight,
    build_current_reference,
    build_device_context,
    build_diagnostic_context,
    build_service_harness,
    build_update_preflight,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.exclusion_policy.enforcement import (
    AllowedPolicyRevisionBinding,
    policy_indeterminate_error,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.object_storage import CanonicalMediaType, ContentDigest
from personal_os.small_file_sync.contracts import (
    MAX_SINGLE_PART_FILE_SIZE_BYTES,
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflightOutcome,
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
)
from personal_os.small_file_sync.errors import SmallFileSyncError
from personal_os.small_file_sync.metrics import SmallFileMetricOutcome, SmallFileRejectionReason
from personal_os.small_file_sync.service import (
    SmallFilePreflightResult,
    derive_create_title,
    derive_source_type,
)
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.results import PublicationOutcome

_EXPIRY_SECONDS: Final[int] = 900
_ALTERNATE_CONTENT: Final[bytes] = b"different canonical bytes for payload substitution\n"


def _error_code(error: ApplicationError) -> ErrorCode:
    return error.error_code


async def _reserve_create_operation(
    harness: ServiceHarness,
) -> tuple[SmallFilePreflightResult, SmallFileDeviceContext]:
    """Run one create preflight and return its result and device context."""

    device_context = build_device_context()
    harness.operation_store.now_override = harness.clock.moment
    result = await harness.service.preflight(
        preflight=build_create_preflight(),
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )
    return result, device_context


class TestPreflightPolicy:
    @pytest.mark.asyncio
    async def test_policy_denial_returns_excluded_before_any_store_or_object_access(
        self,
    ) -> None:
        harness = build_service_harness(denying_policy_guard=True)

        result = await harness.service.preflight(
            preflight=build_create_preflight(),
            device_context=build_device_context(),
            diagnostic_context=build_diagnostic_context(),
        )

        assert result.outcome is SmallFilePreflightOutcome.EXCLUDED
        assert result.terminal_result is None
        assert result.operation_token is None
        assert result.expires_at is None
        # The denial happens before every other port: the ledger stays empty.
        assert harness.ledger.entries == []
        assert (
            harness.metrics.preflight_count(
                SmallFileOperation.CREATE, SmallFilePreflightOutcome.EXCLUDED
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_policy_denial_records_the_closed_code_into_the_rejection_ring(
        self,
    ) -> None:
        """A denial keeps the excluded outcome and now names its closed why."""

        harness = build_service_harness(denying_policy_guard=True)

        result = await harness.service.preflight(
            preflight=build_create_preflight(),
            device_context=build_device_context(),
            diagnostic_context=build_diagnostic_context(),
        )

        assert result.outcome is SmallFilePreflightOutcome.EXCLUDED
        assert (
            harness.metrics.rejection_count(
                SmallFileOperation.CREATE, SmallFileRejectionReason.EXCLUSION_POLICY_DENIED
            )
            == 1
        )
        diagnostics = harness.metrics.rejection_diagnostics()
        (record,) = diagnostics.recent_rejections
        assert record.error_code is SmallFileRejectionReason.EXCLUSION_POLICY_DENIED
        assert record.operation is SmallFileOperation.CREATE

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "system_code",
        [
            ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED,
            ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE,
        ],
    )
    async def test_policy_system_failure_propagates_as_the_typed_error(
        self, system_code: ErrorCode
    ) -> None:
        """A policy SYSTEM failure never collapses into the 200 excluded shape.

        The API's existing closed status mapping renders the typed error as a
        409 (not initialized) or 503 (signing unavailable) envelope, and the
        plugin's wire table maps both onto the retryable server_error family.
        """

        harness = build_service_harness(policy_guard_error=ExclusionPolicyError(system_code))

        with pytest.raises(ExclusionPolicyError) as raised:
            await harness.service.preflight(
                preflight=build_create_preflight(),
                device_context=build_device_context(),
                diagnostic_context=build_diagnostic_context(),
            )

        assert raised.value.error_code is system_code
        # The system failure still names its closed why on the operator ring...
        assert (
            harness.metrics.rejection_count(
                SmallFileOperation.CREATE, SmallFileRejectionReason(system_code.value)
            )
            == 1
        )
        # ...but never masquerades as a completed excluded preflight.
        assert (
            harness.metrics.preflight_count(
                SmallFileOperation.CREATE, SmallFilePreflightOutcome.EXCLUDED
            )
            == 0
        )
        assert harness.ledger.entries == []


class TestPreflightReservation:
    @pytest.mark.asyncio
    async def test_preflight_reserves_with_the_guard_binding_not_the_plugin_revision(self) -> None:
        harness = build_service_harness()
        assert hasattr(harness.policy_guard, "policy_revision_number")
        harness.policy_guard.policy_revision_number = 7
        preflight = build_create_preflight(policy_revision_number=2)

        result = await harness.service.preflight(
            preflight=preflight,
            device_context=build_device_context(),
            diagnostic_context=build_diagnostic_context(),
        )

        assert result.operation_token is not None
        record = harness.operation_store.record_for_token(result.operation_token)
        assert record is not None
        assert record.policy_revision_number == 7

    @pytest.mark.asyncio
    async def test_create_reserves_operation_without_source_insert(self) -> None:
        harness = build_service_harness()

        result, _ = await _reserve_create_operation(harness)

        assert result.outcome is SmallFilePreflightOutcome.SINGLE_PART_UPLOAD
        assert result.operation_token is not None
        assert result.expires_at == harness.clock.moment + timedelta(seconds=_EXPIRY_SECONDS)
        record = harness.operation_store.record_for_token(result.operation_token)
        assert record is not None
        assert record.reserved_source_id is not None
        assert harness.publication_store.source_rows == set()
        assert PUBLICATION_COMMIT_CREATE not in harness.ledger.entries
        assert harness.ledger.entries == [
            SYNC_POLICY_GUARD,
            STORE_RESOLVE_TERMINAL,
            STORE_RESERVE_OPERATION,
        ]
        assert (
            harness.metrics.preflight_count(
                SmallFileOperation.CREATE, SmallFilePreflightOutcome.SINGLE_PART_UPLOAD
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_preflight_accepts_size_exactly_at_the_server_ceiling(self) -> None:
        harness = build_service_harness()
        ceiling_content = b"\x00" * MAX_SINGLE_PART_FILE_SIZE_BYTES

        result = await harness.service.preflight(
            preflight=build_create_preflight(content=ceiling_content),
            device_context=build_device_context(),
            diagnostic_context=build_diagnostic_context(),
        )

        assert result.outcome is SmallFilePreflightOutcome.SINGLE_PART_UPLOAD

    @pytest.mark.asyncio
    async def test_pending_same_identity_preflight_returns_rotated_operation(self) -> None:
        harness = build_service_harness()
        device_context = build_device_context()
        preflight = build_create_preflight()

        first = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        second = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

        assert first.outcome is SmallFilePreflightOutcome.SINGLE_PART_UPLOAD
        assert second.outcome is SmallFilePreflightOutcome.SINGLE_PART_UPLOAD
        assert first.operation_token is not None
        assert second.operation_token is not None
        assert first.operation_token.value != second.operation_token.value
        assert first.expires_at == second.expires_at
        assert harness.ledger.count(STORE_RESERVE_OPERATION) == 2
        assert PUBLICATION_COMMIT_CREATE not in harness.ledger.entries


class TestPreflightUpdateBase:
    @pytest.mark.asyncio
    async def test_stale_base_returns_conflict_without_reservation(self) -> None:
        preflight = build_update_preflight(source_id=uuid4(), base_version_id=uuid4())
        reference = build_current_reference(
            preflight, source_version_id=uuid4(), content_digest=ContentDigest.parse("c" * 64)
        )
        harness = build_service_harness(current_reference=reference)

        result = await harness.service.preflight(
            preflight=preflight,
            device_context=build_device_context(),
            diagnostic_context=build_diagnostic_context(),
        )

        assert result.outcome is SmallFilePreflightOutcome.CONFLICT
        assert result.terminal_result is None
        assert result.operation_token is None
        assert STORE_RESERVE_OPERATION not in harness.ledger.entries
        assert CURRENT_SOURCES_RESOLVE in harness.ledger.entries
        assert PUBLICATION_COMMIT_UPDATE not in harness.ledger.entries
        assert (
            harness.metrics.preflight_count(
                SmallFileOperation.UPDATE, SmallFilePreflightOutcome.CONFLICT
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_missing_update_source_returns_conflict(self) -> None:
        harness = build_service_harness(current_reference=None)
        preflight = build_update_preflight(source_id=uuid4(), base_version_id=uuid4())

        result = await harness.service.preflight(
            preflight=preflight,
            device_context=build_device_context(),
            diagnostic_context=build_diagnostic_context(),
        )

        assert result.outcome is SmallFilePreflightOutcome.CONFLICT
        assert STORE_RESERVE_OPERATION not in harness.ledger.entries
        assert PUBLICATION_COMMIT_UPDATE not in harness.ledger.entries

    @pytest.mark.asyncio
    async def test_changed_content_over_current_base_opens_upload(self) -> None:
        preflight = build_update_preflight(source_id=uuid4(), base_version_id=uuid4())
        reference = build_current_reference(preflight, content_digest=ContentDigest.parse("c" * 64))
        harness = build_service_harness(current_reference=reference)

        result = await harness.service.preflight(
            preflight=preflight,
            device_context=build_device_context(),
            diagnostic_context=build_diagnostic_context(),
        )

        assert result.outcome is SmallFilePreflightOutcome.SINGLE_PART_UPLOAD
        assert result.operation_token is not None


class TestPreflightUpdateBasePolicy:
    """Read-boundary policy failures split denials from system failures.

    The canonical-read boundary re-evaluates the active policy while it
    resolves the update base (spec 14.2). Its typed policy DENIALS — an
    indeterminate subject over locator-dependent rules exactly like the live
    extension-rule incident, or a definite denial — surface as the same 200
    ``excluded`` preflight outcome the authorize boundary produces (spec
    9/10.1), never as an escaping error the route envelope would answer with
    a 403 the plugin parks as ``login_required``. Policy SYSTEM failures
    (no active signed policy, corrupt signing material) propagate as the
    typed error so the API answers with its closed 409/503 envelope (G1).
    """

    @pytest.mark.asyncio
    async def test_read_boundary_indeterminate_maps_to_excluded_without_reservation(
        self,
    ) -> None:
        preflight = build_update_preflight(source_id=uuid4(), base_version_id=uuid4())
        reference = build_current_reference(preflight, content_digest=ContentDigest.parse("c" * 64))
        harness = build_service_harness(current_reference=reference)
        harness.current_sources.resolve_error = policy_indeterminate_error()

        result = await harness.service.preflight(
            preflight=preflight,
            device_context=build_device_context(),
            diagnostic_context=build_diagnostic_context(),
        )

        assert result.outcome is SmallFilePreflightOutcome.EXCLUDED
        assert result.terminal_result is None
        assert result.operation_token is None
        assert result.expires_at is None
        assert STORE_RESERVE_OPERATION not in harness.ledger.entries
        assert PUBLICATION_COMMIT_UPDATE not in harness.ledger.entries
        assert CURRENT_SOURCES_RESOLVE in harness.ledger.entries
        assert (
            harness.metrics.preflight_count(
                SmallFileOperation.UPDATE, SmallFilePreflightOutcome.EXCLUDED
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_read_boundary_denial_maps_to_excluded_without_reservation(self) -> None:
        preflight = build_update_preflight(source_id=uuid4(), base_version_id=uuid4())
        reference = build_current_reference(preflight, content_digest=ContentDigest.parse("c" * 64))
        harness = build_service_harness(current_reference=reference)
        harness.current_sources.resolve_error = ExclusionPolicyError(
            ErrorCode.EXCLUSION_POLICY_DENIED
        )

        result = await harness.service.preflight(
            preflight=preflight,
            device_context=build_device_context(),
            diagnostic_context=build_diagnostic_context(),
        )

        assert result.outcome is SmallFilePreflightOutcome.EXCLUDED
        assert result.terminal_result is None
        assert STORE_RESERVE_OPERATION not in harness.ledger.entries
        assert PUBLICATION_COMMIT_UPDATE not in harness.ledger.entries
        assert (
            harness.metrics.preflight_count(
                SmallFileOperation.UPDATE, SmallFilePreflightOutcome.EXCLUDED
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_read_boundary_indeterminate_records_the_closed_code_into_the_ring(
        self,
    ) -> None:
        """The read boundary's denial keeps excluded and names its closed why."""

        preflight = build_update_preflight(source_id=uuid4(), base_version_id=uuid4())
        reference = build_current_reference(preflight, content_digest=ContentDigest.parse("c" * 64))
        harness = build_service_harness(current_reference=reference)
        harness.current_sources.resolve_error = policy_indeterminate_error()

        result = await harness.service.preflight(
            preflight=preflight,
            device_context=build_device_context(),
            diagnostic_context=build_diagnostic_context(),
        )

        assert result.outcome is SmallFilePreflightOutcome.EXCLUDED
        assert (
            harness.metrics.rejection_count(
                SmallFileOperation.UPDATE, SmallFileRejectionReason.EXCLUSION_POLICY_INDETERMINATE
            )
            == 1
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "system_code",
        [
            ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED,
            ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE,
        ],
    )
    async def test_read_boundary_policy_system_failure_propagates_as_the_typed_error(
        self, system_code: ErrorCode
    ) -> None:
        """The read boundary never collapses a policy system failure either."""

        preflight = build_update_preflight(source_id=uuid4(), base_version_id=uuid4())
        reference = build_current_reference(preflight, content_digest=ContentDigest.parse("c" * 64))
        harness = build_service_harness(current_reference=reference)
        harness.current_sources.resolve_error = ExclusionPolicyError(system_code)

        with pytest.raises(ExclusionPolicyError) as raised:
            await harness.service.preflight(
                preflight=preflight,
                device_context=build_device_context(),
                diagnostic_context=build_diagnostic_context(),
            )

        assert raised.value.error_code is system_code
        assert (
            harness.metrics.rejection_count(
                SmallFileOperation.UPDATE, SmallFileRejectionReason(system_code.value)
            )
            == 1
        )
        assert (
            harness.metrics.preflight_count(
                SmallFileOperation.UPDATE, SmallFilePreflightOutcome.EXCLUDED
            )
            == 0
        )
        assert STORE_RESERVE_OPERATION not in harness.ledger.entries


class TestPreflightNoChange:
    @pytest.mark.asyncio
    async def test_declared_base_content_freezes_no_change_receipt_and_replays(self) -> None:
        device_context = build_device_context()
        preflight = build_update_preflight(source_id=uuid4(), base_version_id=uuid4())
        reference = build_current_reference(preflight)
        harness = build_service_harness(current_reference=reference)
        harness.operation_store.now_override = harness.clock.moment

        result = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

        assert result.outcome is SmallFilePreflightOutcome.NO_CHANGE
        terminal = result.terminal_result
        assert terminal is not None
        assert terminal.result_kind is SmallFileTerminalResultKind.NO_CHANGE
        assert terminal.source_id == preflight.source_id
        assert terminal.source_version_id == preflight.base_version_id
        assert terminal.content_version == reference.content_version
        assert terminal.committed_at == reference.committed_at
        assert PUBLICATION_COMMIT_UPDATE not in harness.ledger.entries
        assert STORE_RESERVE_OPERATION in harness.ledger.entries

        replayed = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

        assert replayed.outcome is SmallFilePreflightOutcome.NO_CHANGE
        assert replayed.terminal_result == terminal
        assert harness.ledger.count(STORE_RESERVE_OPERATION) == 1
        assert harness.metrics.replay_count(SmallFileOperation.UPDATE) == 1


class TestPreflightReplay:
    @pytest.mark.asyncio
    async def test_committed_operation_replays_exactly_without_republication(self) -> None:
        harness = build_service_harness()
        device_context = build_device_context()
        preflight = build_create_preflight()
        harness.operation_store.now_override = harness.clock.moment
        reserved = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None
        committed = await harness.service.receive(
            operation_token=reserved.operation_token,
            device_context=device_context,
            stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
            diagnostic_context=build_diagnostic_context(),
        )

        replayed = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

        assert replayed.outcome is SmallFilePreflightOutcome.COMMITTED_REPLAY
        assert replayed.terminal_result == committed
        assert replayed.terminal_result is not None
        assert replayed.terminal_result.result_kind is SmallFileTerminalResultKind.COMMITTED
        assert harness.ledger.count(STORE_RESERVE_OPERATION) == 1
        assert harness.ledger.count(PUBLICATION_COMMIT_CREATE) == 1
        assert harness.metrics.replay_count(SmallFileOperation.CREATE) == 1

    @pytest.mark.asyncio
    async def test_different_payload_under_same_identity_is_rejected(self) -> None:
        harness = build_service_harness()
        device_context = build_device_context()
        event_id = uuid4()
        idempotency_key = SmallFileIdempotencyKey(str(uuid4()))
        original = build_create_preflight(event_id=event_id, idempotency_key=idempotency_key)
        harness.operation_store.now_override = harness.clock.moment
        reserved = await harness.service.preflight(
            preflight=original,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None
        await harness.service.receive(
            operation_token=reserved.operation_token,
            device_context=device_context,
            stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
            diagnostic_context=build_diagnostic_context(),
        )
        substituted = build_create_preflight(
            content=_ALTERNATE_CONTENT,
            event_id=event_id,
            idempotency_key=idempotency_key,
        )

        with pytest.raises(SmallFileSyncError) as exc_info:
            await harness.service.preflight(
                preflight=substituted,
                device_context=device_context,
                diagnostic_context=build_diagnostic_context(),
            )

        assert _error_code(exc_info.value) is ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH
        assert harness.ledger.count(STORE_RESERVE_OPERATION) == 1
        assert (
            harness.metrics.rejection_count(
                SmallFileOperation.CREATE,
                SmallFileRejectionReason.SMALL_FILE_OPERATION_IDENTITY_MISMATCH,
            )
            == 1
        )


class TestReceivePublication:
    @pytest.mark.asyncio
    async def test_receive_reconstructs_binding_only_from_the_bound_operation(self) -> None:
        harness = build_service_harness()
        device_context = build_device_context()
        preflight = build_create_preflight(policy_revision_number=7)
        reserved = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None
        record = harness.operation_store.record_for_token(reserved.operation_token)
        assert record is not None
        record.policy_revision_number = 12

        await harness.service.receive(
            operation_token=reserved.operation_token,
            device_context=device_context,
            stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
            diagnostic_context=build_diagnostic_context(),
        )

        assert harness.publication_gateway.bindings == [
            AllowedPolicyRevisionBinding(
                workspace_id=device_context.workspace_id,
                policy_revision_number=12,
            )
        ]

    @pytest.mark.asyncio
    async def test_receive_rejects_binding_workspace_mismatch_before_gateway(self) -> None:
        harness = build_service_harness()
        reserved, device_context = await _reserve_create_operation(harness)
        assert reserved.operation_token is not None
        harness.operation_store.bound_workspace_id_override = uuid4()

        with pytest.raises(SmallFileSyncError) as exc_info:
            await harness.service.receive(
                operation_token=reserved.operation_token,
                device_context=device_context,
                stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
                diagnostic_context=build_diagnostic_context(),
            )

        assert _error_code(exc_info.value) is ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID
        assert harness.publication_gateway.bindings == []

    @pytest.mark.asyncio
    async def test_receive_publishes_create_once_and_freezes_terminal_receipt(self) -> None:
        harness = build_service_harness()
        device_context = build_device_context()
        preflight = build_create_preflight()
        harness.operation_store.now_override = harness.clock.moment
        reserved = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None
        record = harness.operation_store.record_for_token(reserved.operation_token)
        assert record is not None and record.reserved_source_id is not None

        terminal = await harness.service.receive(
            operation_token=reserved.operation_token,
            device_context=device_context,
            stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
            diagnostic_context=build_diagnostic_context(),
        )

        assert terminal.result_kind is SmallFileTerminalResultKind.COMMITTED
        assert terminal.source_id == record.reserved_source_id
        assert harness.publication_store.source_rows == {record.reserved_source_id}
        assert harness.ledger.entries[-7:] == [
            STORE_RESOLVE_BOUND,
            OBJECT_STORE_STORE_STREAM,
            PUBLICATION_GUARD,
            PUBLICATION_RESOLVE_COMMITTED,
            OBJECT_STORE_RESOLVE,
            PUBLICATION_COMMIT_CREATE,
            STORE_RECORD_BOUND_TERMINAL,
        ]
        assert (
            harness.metrics.upload_count(
                SmallFileOperation.CREATE, SmallFileMetricOutcome.COMMITTED
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_publication_no_change_maps_to_no_change_terminal(self) -> None:
        preflight = build_update_preflight(source_id=uuid4(), base_version_id=uuid4())
        reference = build_current_reference(preflight, content_digest=ContentDigest.parse("c" * 64))
        harness = build_service_harness(
            current_reference=reference, update_outcome=PublicationOutcome.NO_CHANGE
        )
        device_context = build_device_context()
        reserved = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None

        terminal = await harness.service.receive(
            operation_token=reserved.operation_token,
            device_context=device_context,
            stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
            diagnostic_context=build_diagnostic_context(),
        )

        assert terminal.result_kind is SmallFileTerminalResultKind.NO_CHANGE
        assert terminal.source_id == preflight.source_id


class TestReceiveIntegrity:
    @pytest.mark.parametrize(
        ("chunks", "subject"),
        [
            ([b"x" * len(SYNC_CONTENT_BYTES)], "digest mismatch"),
            ([b"short"], "size mismatch"),
        ],
    )
    @pytest.mark.asyncio
    async def test_content_mismatch_never_publishes(
        self, chunks: list[bytes], subject: str
    ) -> None:
        del subject
        harness = build_service_harness()
        reserved, device_context = await _reserve_create_operation(harness)
        assert reserved.operation_token is not None

        with pytest.raises(SmallFileSyncError) as exc_info:
            await harness.service.receive(
                operation_token=reserved.operation_token,
                device_context=device_context,
                stream=ProbedByteStream(chunks),
                diagnostic_context=build_diagnostic_context(),
            )

        assert _error_code(exc_info.value) is ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED
        assert harness.publication_store.commit_invocations == 0
        assert harness.publication_store.source_rows == set()
        assert STORE_RECORD_BOUND_TERMINAL not in harness.ledger.entries
        assert (
            harness.metrics.upload_count(
                SmallFileOperation.CREATE, SmallFileMetricOutcome.INTEGRITY_FAILED
            )
            == 1
        )
        assert (
            harness.metrics.rejection_count(
                SmallFileOperation.CREATE,
                SmallFileRejectionReason.SMALL_FILE_CONTENT_INTEGRITY_FAILED,
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_policy_denial_during_publication_never_commits(self) -> None:
        harness = build_service_harness(
            publication_guard_error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)
        )
        device_context = build_device_context()
        preflight = build_create_preflight()
        harness.operation_store.now_override = harness.clock.moment
        reserved = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None

        with pytest.raises(ExclusionPolicyError):
            await harness.service.receive(
                operation_token=reserved.operation_token,
                device_context=device_context,
                stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
                diagnostic_context=build_diagnostic_context(),
            )

        assert harness.publication_store.commit_invocations == 0
        assert STORE_RECORD_BOUND_TERMINAL not in harness.ledger.entries
        assert (
            harness.metrics.upload_count(SmallFileOperation.CREATE, SmallFileMetricOutcome.REJECTED)
            == 1
        )


class TestReceiveTypedRejectionTerminalization:
    @pytest.mark.asyncio
    async def test_typed_non_retryable_rejection_terminalizes_the_claimed_operation(
        self,
    ) -> None:
        """A typed non-retryable rejection never leaves the claim fenced in receiving.

        The publication boundary's typed locator conflict (the stuck 409 of
        the live event) propagates to the caller as the exact same error
        object, while the claimed operation row lands its terminal ``failed``
        state carrying only the closed registry token (child-six deferred
        remediation task 1).
        """

        rejection = SourcePublicationError(ErrorCode.SOURCE_LOCATOR_CONFLICT)
        harness = build_service_harness(publication_guard_error=rejection)
        device_context = build_device_context()
        preflight = build_create_preflight()
        harness.operation_store.now_override = harness.clock.moment
        reserved = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None

        with pytest.raises(ApplicationError) as exc_info:
            await harness.service.receive(
                operation_token=reserved.operation_token,
                device_context=device_context,
                stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
                diagnostic_context=build_diagnostic_context(),
            )

        assert exc_info.value is rejection
        assert _error_code(exc_info.value) is ErrorCode.SOURCE_LOCATOR_CONFLICT
        record = harness.operation_store.record_for_token(reserved.operation_token)
        assert record is not None
        assert record.state == "failed"
        assert record.safe_error_code == "source_locator_conflict"
        assert harness.ledger.entries[-1] == STORE_RECORD_BOUND_TERMINAL_FAILURE
        assert (
            harness.metrics.upload_count(SmallFileOperation.CREATE, SmallFileMetricOutcome.REJECTED)
            == 1
        )

    @pytest.mark.asyncio
    async def test_retryable_typed_failure_keeps_the_operation_receiving(self) -> None:
        """A retryable typed failure retains its current resume behavior.

        The outcome-unknown family stays retryable by contract, so the claim
        must not be terminalized: the row remains ``receiving`` for the
        bounded foreground retry.
        """

        harness = build_service_harness(
            publication_guard_error=SourcePublicationError(ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN)
        )
        device_context = build_device_context()
        harness.operation_store.now_override = harness.clock.moment
        reserved = await harness.service.preflight(
            preflight=build_create_preflight(),
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None

        with pytest.raises(ApplicationError) as exc_info:
            await harness.service.receive(
                operation_token=reserved.operation_token,
                device_context=device_context,
                stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
                diagnostic_context=build_diagnostic_context(),
            )

        assert _error_code(exc_info.value) is ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN
        record = harness.operation_store.record_for_token(reserved.operation_token)
        assert record is not None
        assert record.state == "receiving"
        assert record.safe_error_code is None
        assert STORE_RECORD_BOUND_TERMINAL_FAILURE not in harness.ledger.entries

    @pytest.mark.asyncio
    async def test_untyped_failure_keeps_the_operation_receiving(self) -> None:
        """An untyped failure is never terminalized by the receive path."""

        harness = build_service_harness(
            publication_guard_error=RuntimeError("untyped transport failure")
        )
        device_context = build_device_context()
        harness.operation_store.now_override = harness.clock.moment
        reserved = await harness.service.preflight(
            preflight=build_create_preflight(),
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None

        with pytest.raises(RuntimeError):
            await harness.service.receive(
                operation_token=reserved.operation_token,
                device_context=device_context,
                stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
                diagnostic_context=build_diagnostic_context(),
            )

        record = harness.operation_store.record_for_token(reserved.operation_token)
        assert record is not None
        assert record.state == "receiving"
        assert record.safe_error_code is None
        assert STORE_RECORD_BOUND_TERMINAL_FAILURE not in harness.ledger.entries


class TestReceiveGuards:
    @pytest.mark.asyncio
    async def test_expired_operation_fails_closed_without_object_access(self) -> None:
        harness = build_service_harness()
        device_context = build_device_context()
        preflight = build_create_preflight()
        harness.operation_store.now_override = harness.clock.moment
        reserved = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None
        harness.operation_store.now_override = harness.clock.moment + timedelta(
            seconds=_EXPIRY_SECONDS + 1
        )

        with pytest.raises(SmallFileSyncError) as exc_info:
            await harness.service.receive(
                operation_token=reserved.operation_token,
                device_context=device_context,
                stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
                diagnostic_context=build_diagnostic_context(),
            )

        assert _error_code(exc_info.value) is ErrorCode.SMALL_FILE_OPERATION_EXPIRED
        assert OBJECT_STORE_STORE_STREAM not in harness.ledger.entries
        assert harness.publication_store.commit_invocations == 0

    @pytest.mark.asyncio
    async def test_size_ceiling_enforced_before_any_spool(self) -> None:
        harness = build_service_harness()
        device_context = build_device_context()
        preflight = build_create_preflight()
        harness.operation_store.now_override = harness.clock.moment
        reserved = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None
        harness.operation_store.declared_size_override_bytes = MAX_SINGLE_PART_FILE_SIZE_BYTES + 1

        with pytest.raises(SmallFileSyncError) as exc_info:
            await harness.service.receive(
                operation_token=reserved.operation_token,
                device_context=device_context,
                stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
                diagnostic_context=build_diagnostic_context(),
            )

        assert _error_code(exc_info.value) is ErrorCode.SMALL_FILE_SIZE_LIMIT_EXCEEDED
        assert OBJECT_STORE_STORE_STREAM not in harness.ledger.entries
        assert harness.publication_store.commit_invocations == 0
        assert (
            harness.metrics.rejection_count(
                SmallFileOperation.CREATE, SmallFileRejectionReason.SMALL_FILE_SIZE_LIMIT_EXCEEDED
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_stale_rotated_token_surfaces_operation_not_found(self) -> None:
        harness = build_service_harness()
        device_context = build_device_context()
        preflight = build_create_preflight()
        harness.operation_store.now_override = harness.clock.moment
        first = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        second = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert first.operation_token is not None
        assert second.operation_token is not None

        with pytest.raises(SmallFileSyncError) as exc_info:
            await harness.service.receive(
                operation_token=first.operation_token,
                device_context=device_context,
                stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
                diagnostic_context=build_diagnostic_context(),
            )

        assert _error_code(exc_info.value) is ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND


class TestReceiveReplayAndConcurrency:
    @pytest.mark.asyncio
    async def test_locator_reauthorization_rebinds_claimed_exact_token_before_resume(
        self,
    ) -> None:
        """A crash after claim resumes the same token under fresh locator authority."""

        harness = build_service_harness()
        device_context = build_device_context()
        preflight = build_create_preflight()
        reserved = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None

        # Model interruption after the PUT claimed the durable row but before
        # any canonical publication transaction began.
        claimed = await harness.operation_store.resolve_bound_operation(
            reserved.operation_token,
            device_context,
            build_diagnostic_context(),
        )
        next_revision = claimed.policy_revision_number + 1
        assert hasattr(harness.policy_guard, "policy_revision_number")
        harness.policy_guard.policy_revision_number = next_revision

        with pytest.raises(SmallFileSyncError) as retry_required:
            await harness.service.preflight(
                preflight=preflight,
                device_context=device_context,
                diagnostic_context=build_diagnostic_context(),
            )
        assert _error_code(retry_required.value) is ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID

        rebound = harness.operation_store.record_for_token(reserved.operation_token)
        assert rebound is not None
        assert rebound.state == "receiving"
        assert rebound.operation_token == reserved.operation_token
        assert rebound.policy_revision_number == next_revision

        terminal = await harness.service.receive(
            operation_token=reserved.operation_token,
            device_context=device_context,
            stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
            diagnostic_context=build_diagnostic_context(),
        )

        assert terminal.result_kind is SmallFileTerminalResultKind.COMMITTED
        assert harness.publication_gateway.bindings[-1].policy_revision_number == next_revision
        assert harness.publication_store.commit_invocations == 1

    @pytest.mark.asyncio
    async def test_unbound_terminal_write_cannot_bypass_a_claimed_receive(self) -> None:
        harness = build_service_harness()
        device_context = build_device_context()
        preflight = build_create_preflight()
        operation = await harness.operation_store.reserve_operation(
            preflight,
            device_context,
            AllowedPolicyRevisionBinding(
                workspace_id=device_context.workspace_id,
                policy_revision_number=7,
            ),
            build_diagnostic_context(),
        )
        bound = await harness.operation_store.resolve_bound_operation(
            operation.operation_token,
            device_context,
            build_diagnostic_context(),
        )

        with pytest.raises(SmallFileSyncError) as exc_info:
            await harness.operation_store.record_terminal_result(
                operation,
                SmallFileTerminalResult(
                    result_kind=SmallFileTerminalResultKind.COMMITTED,
                    source_id=uuid4(),
                    source_version_id=uuid4(),
                    content_version=1,
                    committed_at=harness.clock.moment,
                ),
                build_diagnostic_context(),
            )

        assert _error_code(exc_info.value) is ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID
        await harness.operation_store.record_bound_terminal_result(
            bound,
            SmallFileTerminalResult(
                result_kind=SmallFileTerminalResultKind.COMMITTED,
                source_id=uuid4(),
                source_version_id=uuid4(),
                content_version=1,
                committed_at=harness.clock.moment,
            ),
            build_diagnostic_context(),
        )

    @pytest.mark.asyncio
    async def test_receive_claim_survives_expiry_until_terminal(
        self,
    ) -> None:
        harness = build_service_harness()
        device_context = build_device_context()
        preflight = build_create_preflight()
        harness.operation_store.now_override = harness.clock.moment
        reserved = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None
        record = harness.operation_store.record_for_token(reserved.operation_token)
        assert record is not None
        claimed_revision = record.policy_revision_number

        entered = asyncio.Event()
        release = asyncio.Event()
        harness.publication_gateway.entered_by_revision = {claimed_revision: entered}
        harness.publication_gateway.release_by_revision = {claimed_revision: release}
        receive_task = asyncio.create_task(
            harness.service.receive(
                operation_token=reserved.operation_token,
                device_context=device_context,
                stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
                diagnostic_context=build_diagnostic_context(),
            )
        )
        await entered.wait()

        # The reservation deadline may pass after the receive has already
        # claimed the operation and entered canonical publication. That claim
        # must retain its token/revision fence until guarded terminalization.
        harness.operation_store.now_override = record.expires_at + timedelta(seconds=1)

        release.set()
        terminal = await receive_task

        committed = harness.operation_store.record_for_token(reserved.operation_token)
        assert committed is not None
        assert committed.state == "committed"
        assert committed.policy_revision_number == claimed_revision
        assert terminal.result_kind is SmallFileTerminalResultKind.COMMITTED

        replay = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert replay.outcome is SmallFilePreflightOutcome.COMMITTED_REPLAY

    @pytest.mark.asyncio
    async def test_concurrent_receives_keep_their_policy_bindings_isolated(self) -> None:
        harness = build_service_harness()
        device_context = build_device_context()
        first_reserved = await harness.service.preflight(
            preflight=build_create_preflight(),
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        second_reserved = await harness.service.preflight(
            preflight=build_create_preflight(),
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert first_reserved.operation_token is not None
        assert second_reserved.operation_token is not None
        first_record = harness.operation_store.record_for_token(first_reserved.operation_token)
        second_record = harness.operation_store.record_for_token(second_reserved.operation_token)
        assert first_record is not None
        assert second_record is not None
        first_record.policy_revision_number = 11
        second_record.policy_revision_number = 12

        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        first_release = asyncio.Event()
        second_release = asyncio.Event()
        harness.publication_gateway.entered_by_revision = {
            11: first_entered,
            12: second_entered,
        }
        harness.publication_gateway.release_by_revision = {
            11: first_release,
            12: second_release,
        }
        first_task = asyncio.create_task(
            harness.service.receive(
                operation_token=first_reserved.operation_token,
                device_context=device_context,
                stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
                diagnostic_context=build_diagnostic_context(),
            )
        )
        second_task = asyncio.create_task(
            harness.service.receive(
                operation_token=second_reserved.operation_token,
                device_context=device_context,
                stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
                diagnostic_context=build_diagnostic_context(),
            )
        )
        await first_entered.wait()
        await second_entered.wait()

        second_release.set()
        second_terminal = await second_task
        first_release.set()
        first_terminal = await first_task

        assert {
            binding.policy_revision_number for binding in harness.publication_gateway.bindings
        } == {11, 12}
        assert first_terminal.result_kind is SmallFileTerminalResultKind.COMMITTED
        assert second_terminal.result_kind is SmallFileTerminalResultKind.COMMITTED

    @pytest.mark.asyncio
    async def test_response_loss_replays_frozen_terminal_without_second_publication(
        self,
    ) -> None:
        harness = build_service_harness()
        device_context = build_device_context()
        preflight = build_create_preflight()
        harness.operation_store.now_override = harness.clock.moment
        reserved = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None
        committed = await harness.service.receive(
            operation_token=reserved.operation_token,
            device_context=device_context,
            stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
            diagnostic_context=build_diagnostic_context(),
        )

        replayed = await harness.service.receive(
            operation_token=reserved.operation_token,
            device_context=device_context,
            stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
            diagnostic_context=build_diagnostic_context(),
        )

        assert replayed == committed
        assert harness.ledger.count(PUBLICATION_COMMIT_CREATE) == 1
        assert len(harness.publication_store.source_rows) == 1
        assert harness.metrics.replay_count(SmallFileOperation.CREATE) == 1

    @pytest.mark.asyncio
    async def test_concurrent_receives_produce_exactly_one_publication(self) -> None:
        harness = build_service_harness()
        device_context = build_device_context()
        preflight = build_create_preflight()
        harness.operation_store.now_override = harness.clock.moment
        reserved = await harness.service.preflight(
            preflight=preflight,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
        assert reserved.operation_token is not None

        first, second = await asyncio.gather(
            harness.service.receive(
                operation_token=reserved.operation_token,
                device_context=device_context,
                stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
                diagnostic_context=build_diagnostic_context(),
            ),
            harness.service.receive(
                operation_token=reserved.operation_token,
                device_context=device_context,
                stream=ProbedByteStream([SYNC_CONTENT_BYTES]),
                diagnostic_context=build_diagnostic_context(),
            ),
        )

        assert first == second
        assert len(harness.publication_store.committed_fingerprints) == 1
        assert len(harness.publication_store.source_rows) == 1
        assert first.result_kind is SmallFileTerminalResultKind.COMMITTED


class TestCreateDerivation:
    @pytest.mark.parametrize(
        ("media_type_value", "expected_type"),
        [
            ("text/markdown", "markdown"),
            ("text/plain", "text"),
            ("application/pdf", "pdf"),
            ("image/png", "image"),
            ("audio/ogg", "audio"),
            ("application/octet-stream", "text"),
        ],
    )
    def test_source_type_maps_from_the_closed_media_type_vocabulary(
        self, media_type_value: str, expected_type: str
    ) -> None:
        assert derive_source_type(CanonicalMediaType.parse(media_type_value)).value == (
            expected_type
        )

    def test_create_title_is_a_stable_valid_label(self) -> None:
        title = derive_create_title(CanonicalMediaType.parse("text/markdown"))

        assert title.value == "Markdown file"
        assert derive_create_title(CanonicalMediaType.parse("text/markdown")) == title
