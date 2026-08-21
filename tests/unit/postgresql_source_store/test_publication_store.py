"""Publication-store consumption of transaction-final policy evidence."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final, cast
from uuid import UUID, uuid4

import pytest
from tests.unit.sources.fakes import (
    build_committed_result,
    build_create_command,
    build_diagnostic_context,
    build_policy_decision,
    build_verified_receipt,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.exclusion_policy.enforcement import (
    AllowedPolicyRevisionBinding,
    PublicationPolicyEvidence,
)
from personal_os.sources.commands import CreateSourceVersion
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.fingerprint import compute_request_fingerprint
from personal_os.sources.results import SourceVersionPublicationResult
from postgresql_source_store import publication_store
from postgresql_source_store.publication_store import PostgresqlSourcePublicationStore


class _AsyncContext:
    def __init__(self, entered: object) -> None:
        self._entered = entered

    async def __aenter__(self) -> object:
        return self._entered

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc_value, traceback


class _RecordingConnection:
    def __init__(self) -> None:
        self.executed_statements: list[object] = []

    def begin(self) -> _AsyncContext:
        return _AsyncContext(None)

    async def execute(self, statement: object) -> object:
        self.executed_statements.append(statement)
        return object()


class _Engine:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    def connect(self) -> _AsyncContext:
        return _AsyncContext(self._connection)


class _AcceptingVerifier:
    def verify(
        self,
        *,
        public_key_bytes: bytes,
        signature_bytes: bytes,
        message: bytes,
    ) -> bool:
        del public_key_bytes, signature_bytes, message
        return True


class _RecordingOperationFence:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def acquire_bound_publication_fence_in_transaction(
        self, connection: object, bound: object
    ) -> None:
        del connection, bound
        self.order.append("operation_fence")

    async def record_bound_terminal_result_in_transaction(
        self, connection: object, bound: object, result: object
    ) -> None:
        del connection, bound, result
        self.order.append("operation_terminal")


class _ControlledStore(PostgresqlSourcePublicationStore):
    def __init__(self, command: CreateSourceVersion, *, with_operation_fence: bool = False) -> None:
        self.connection = _RecordingConnection()
        self.order: list[str] = []
        self.subject = PolicySubject(
            workspace_id=command.workspace_id,
            source_id=command.source_id,
            source_type=command.source_type,
            media_type=command.expected_object.media_type,
            size_bytes=command.expected_object.size_bytes,
        )
        super().__init__(
            cast(Any, _Engine(self.connection)),
            policy_verifier=_AcceptingVerifier(),
            small_file_operation_store=(
                cast(Any, _RecordingOperationFence(self.order)) if with_operation_fence else None
            ),
            small_file_bound_operation=cast(Any, object()) if with_operation_fence else None,
        )

    async def _select_workspace_is_active(self, connection: object, workspace_id: UUID) -> bool:
        del connection, workspace_id
        return True

    async def _is_actor_valid(self, connection: object, command: object) -> bool:
        del connection, command
        return True

    async def _resolve_identity(
        self,
        connection: object,
        command: object,
        request_fingerprint: object,
    ) -> tuple[None, None]:
        del connection, command, request_fingerprint
        return None, None

    async def _build_authoritative_subject(
        self,
        connection: object,
        command: object,
        receipt: object,
        bound_locator: object = None,
    ) -> PolicySubject:
        del connection, command, receipt, bound_locator
        self.order.append("subject")
        return self.subject


async def _run_transition(
    store: _ControlledStore,
    command: CreateSourceVersion,
    evidence: PublicationPolicyEvidence,
    transition: Callable[
        [object],
        Awaitable[tuple[None, SourceVersionPublicationResult]],
    ],
) -> SourceVersionPublicationResult:
    return await store._run_locked_transition(
        command,
        compute_request_fingerprint(command),
        build_verified_receipt(
            command.expected_object,
            build_committed_result(command).committed_at,
        ),
        build_diagnostic_context(),
        cast(Any, transition),
        preflight_decision=evidence,
    )


@pytest.mark.asyncio
async def test_store_passes_bound_evidence_to_locked_authorization_before_source_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = build_create_command()
    binding = AllowedPolicyRevisionBinding(command.workspace_id, 7)
    store = _ControlledStore(command)
    committed = build_committed_result(command)
    helper_evidence: list[PublicationPolicyEvidence | None] = []

    async def authorize_locked(*args: object, **kwargs: object) -> PublicationPolicyEvidence:
        del args
        store.order.append("policy")
        helper_evidence.append(cast(PublicationPolicyEvidence | None, kwargs["policy_evidence"]))
        assert kwargs["command"] is command
        assert kwargs["subject"] is store.subject
        return binding

    async def legacy_authorize(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return build_policy_decision(workspace_id=command.workspace_id)

    async def transition(
        connection: object,
    ) -> tuple[None, SourceVersionPublicationResult]:
        del connection
        store.order.append("transition")
        return None, committed

    monkeypatch.setattr(
        publication_store,
        "authorize_locked_publication_policy",
        authorize_locked,
        raising=False,
    )
    monkeypatch.setattr(
        publication_store,
        "evaluate_locked_policy_decision",
        legacy_authorize,
        raising=False,
    )

    result = await _run_transition(store, command, binding, transition)

    assert result is committed
    assert helper_evidence == [binding]
    assert helper_evidence[0] is binding
    assert store.order == ["subject", "policy", "transition"]


@pytest.mark.asyncio
async def test_small_file_fence_wraps_canonical_transition_and_terminalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = build_create_command()
    binding = AllowedPolicyRevisionBinding(command.workspace_id, 7)
    store = _ControlledStore(command, with_operation_fence=True)
    committed = build_committed_result(command)

    async def authorize_locked(*args: object, **kwargs: object) -> PublicationPolicyEvidence:
        del args, kwargs
        store.order.append("policy")
        return binding

    async def transition(
        connection: object,
    ) -> tuple[None, SourceVersionPublicationResult]:
        del connection
        store.order.append("transition")
        return None, committed

    monkeypatch.setattr(
        publication_store,
        "authorize_locked_publication_policy",
        authorize_locked,
    )

    result = await _run_transition(store, command, binding, transition)

    assert result is committed
    assert store.order == [
        "operation_fence",
        "subject",
        "policy",
        "transition",
        "operation_terminal",
    ]


@pytest.mark.asyncio
async def test_foreign_workspace_binding_fails_before_source_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = build_create_command()
    binding = AllowedPolicyRevisionBinding(uuid4(), 7)
    store = _ControlledStore(command)
    transition_calls = 0

    async def unexpected_authorization(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("foreign evidence reached locked policy authorization")

    async def transition(
        connection: object,
    ) -> tuple[None, SourceVersionPublicationResult]:
        nonlocal transition_calls
        del connection
        transition_calls += 1
        return None, build_committed_result(command)

    monkeypatch.setattr(
        publication_store,
        "authorize_locked_publication_policy",
        unexpected_authorization,
        raising=False,
    )
    monkeypatch.setattr(
        publication_store,
        "evaluate_locked_policy_decision",
        unexpected_authorization,
        raising=False,
    )

    with pytest.raises(SourcePublicationError) as raised:
        await _run_transition(store, command, binding, transition)

    assert raised.value.error_code is ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED
    assert store.order == []
    assert transition_calls == 0


class _LocatorBoundStore(_ControlledStore):
    """Publication store double that builds the subject from the bound locator.

    The double mirrors the durable
    :meth:`PostgresqlSourcePublicationStore._build_authoritative_subject`:
    a small-file create carries its bound initial locator onto the policy
    subject so the locked guard reevaluates the locator-aware rule under
    the current revision. The double records every argument and the
    emitted subject so the test can assert the wiring exactly.
    """

    def __init__(self, command: CreateSourceVersion) -> None:
        super().__init__(command)
        self.bound_locator_argument: object = _MISSING
        self.observed_subject: PolicySubject | None = None

    async def _build_authoritative_subject(
        self,
        connection: object,
        command: object,
        receipt: object,
        bound_locator: object = None,
    ) -> PolicySubject:
        del connection, command, receipt
        self.bound_locator_argument = bound_locator
        self.order.append("subject")
        normalized_locator_value: str | None = None
        if bound_locator is not None:
            normalized_locator_value = bound_locator.value
        self.observed_subject = PolicySubject(
            workspace_id=self.subject.workspace_id,
            source_id=self.subject.source_id,
            source_type=self.subject.source_type,
            normalized_locator=normalized_locator_value,
            media_type=self.subject.media_type,
            size_bytes=self.subject.size_bytes,
        )
        return self.observed_subject


@pytest.mark.asyncio
async def test_durable_publication_store_carries_bound_locator_into_locked_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small-file create carries its bound locator through the locked guard.

    The brief requires the publication guard to reevaluate the bound
    locator under the locked current policy. The offline composition's
    :meth:`PolicyEnforcementService._publication_subject` is intentionally
    locator-free (the offline wire test asserts the closed indeterminate
    failure there). The durable
    :meth:`PostgresqlSourcePublicationStore._build_authoritative_subject`
    is the authoritative path — it surfaces the bound locator on the
    subject that reaches :func:`authorize_locked_publication_policy`, so a
    folder rule that excludes the locator can reach a definite denial.
    """

    from personal_os.source_locators.values import NormalizedLocator

    locator = NormalizedLocator("notes/foo.md")
    command = build_create_command(initial_locator=locator)
    binding = AllowedPolicyRevisionBinding(command.workspace_id, 7)
    store = _LocatorBoundStore(command)
    committed = build_committed_result(command)
    captured_subjects: list[PolicySubject] = []

    async def authorize_locked(*args: object, **kwargs: object) -> PublicationPolicyEvidence:
        del args
        store.order.append("policy")
        captured_subjects.append(cast(PolicySubject, kwargs["subject"]))
        return binding

    async def transition(
        connection: object,
    ) -> tuple[None, SourceVersionPublicationResult]:
        del connection
        store.order.append("transition")
        return None, committed

    monkeypatch.setattr(
        publication_store,
        "authorize_locked_publication_policy",
        authorize_locked,
        raising=False,
    )

    result = await _run_transition(store, command, binding, transition)

    assert result is committed
    # The bound locator reaches the subject-building path verbatim.
    assert store.bound_locator_argument is locator
    # The subject the locked guard sees carries the bound locator.
    assert len(captured_subjects) == 1
    assert captured_subjects[0].normalized_locator == "notes/foo.md"
    assert captured_subjects[0].workspace_id == command.workspace_id
    assert captured_subjects[0].source_id == command.source_id
    assert store.order == ["subject", "policy", "transition"]


_MISSING: Final[object] = object()
