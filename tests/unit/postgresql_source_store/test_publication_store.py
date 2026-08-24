"""Publication-store consumption of transaction-final policy evidence."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Final, cast
from uuid import UUID, uuid4

import psycopg
import pytest
import sqlalchemy.exc as sa_exc
from sqlalchemy.dialects import postgresql as sa_postgresql
from tests.unit.sources.fakes import (
    build_committed_result,
    build_create_command,
    build_diagnostic_context,
    build_policy_decision,
    build_verified_receipt,
)

from personal_os.error_contracts.codes import ErrorCategory, ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.exclusion_policy.enforcement import (
    AllowedPolicyRevisionBinding,
    PublicationPolicyEvidence,
)
from personal_os.object_storage import VerifiedObjectReceipt
from personal_os.source_locators.values import NormalizedLocator
from personal_os.sources.commands import CreateSourceVersion
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.fingerprint import compute_request_fingerprint
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult
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


# --- durable create transition over a scripted engine -----------------------------


_ACTIVE_LOCATOR_CONSTRAINT: Final[str] = "uq_source_locators_active_workspace_path"
_COMMIT_VERIFIED_AT: Final[datetime] = datetime(2026, 8, 23, 16, 0, 0, tzinfo=UTC)
_PG_DIALECT: Final[Any] = sa_postgresql.dialect()


def _unique_violation(constraint_name: str, table_name: str) -> sa_exc.IntegrityError:
    """The SQLAlchemy-wrapped psycopg 23505 the scripted table raises."""

    return sa_exc.IntegrityError(
        f"INSERT INTO knowledge.{table_name} ...",
        {},
        psycopg.errors.UniqueViolation(
            f"duplicate key value violates unique constraint {constraint_name!r}"
        ),
    )


class _CreateScriptedResult:
    """Minimal async result double for one scripted create-transition statement."""

    def __init__(
        self,
        *,
        scalar: object | None = None,
        row: object | None = None,
        rowcount: int = 0,
    ) -> None:
        self._scalar = scalar
        self._row = row
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> object | None:
        return self._scalar

    def one_or_none(self) -> object | None:
        return self._row

    def one(self) -> object:
        assert self._row is not None, "scripted result must program exactly one row"
        return self._row


class _CreateScriptedConnection:
    """Connection double serving the durable create transition's statements.

    The double models only the canonical state the create's bound initial
    locator depends on: the foreign owner of an ACTIVE locator at the command's
    bound path (``closed_event_id IS NULL`` under the partial unique index),
    and whether the initial-locator INSERT itself violates that index. Every
    other table accepts the write; the content-object reuse lookup returns a
    row matching the receipt exactly; the guarded pointer update matches one
    row; the create event returns sequence 1.
    """

    def __init__(
        self,
        receipt: VerifiedObjectReceipt,
        *,
        foreign_active_locator_source_id: UUID | None,
        locator_insert_violates: bool,
        sources_insert_violates: bool = False,
    ) -> None:
        self._receipt = receipt
        self._foreign_active_locator_source_id = foreign_active_locator_source_id
        self._locator_insert_violates = locator_insert_violates
        self._sources_insert_violates = sources_insert_violates
        self.locator_prechecks = 0
        self.locator_inserts = 0
        self.audit_rows: list[tuple[str | None, str | None]] = []

    def begin(self) -> _AsyncContext:
        return _AsyncContext(None)

    async def execute(self, statement: object) -> object:
        visit_name = statement.__visit_name__  # type: ignore[attr-defined]
        if visit_name in {"text", "textclause"}:
            # Transaction bounds and the transaction-scoped advisory locks.
            return _CreateScriptedResult()
        compiled = str(statement.compile(dialect=_PG_DIALECT))  # type: ignore[attr-defined]
        if visit_name == "select":
            if "FROM knowledge.source_locators" in compiled:
                self.locator_prechecks += 1
                return _CreateScriptedResult(scalar=self._foreign_active_locator_source_id)
            if "FROM knowledge.sources" in compiled:
                # ``_select_source_workspace_id``: the global primary key is free.
                return _CreateScriptedResult(scalar=None)
            if "FROM knowledge.content_objects" in compiled:
                return _CreateScriptedResult(
                    row=SimpleNamespace(
                        content_object_id=uuid4(),
                        object_key=self._receipt.object_key.value,
                        byte_size=self._receipt.size_bytes,
                        media_type=self._receipt.media_type.value,
                    )
                )
            raise AssertionError(f"unexpected select: {compiled}")
        if visit_name == "insert":
            if "INTO knowledge.source_locators" in compiled:
                self.locator_inserts += 1
                if self._locator_insert_violates:
                    raise _unique_violation(_ACTIVE_LOCATOR_CONSTRAINT, "source_locators")
                return _CreateScriptedResult()
            if "INTO knowledge.sources" in compiled:
                if self._sources_insert_violates:
                    raise _unique_violation("pk_sources", "sources")
                return _CreateScriptedResult()
            if "INTO knowledge.audit_events" in compiled:
                params = statement.compile(dialect=_PG_DIALECT).params  # type: ignore[attr-defined]
                self.audit_rows.append(
                    (params.get("action"), params.get("reason_code")),
                )
                return _CreateScriptedResult()
            # Pending source, version 1, create event and projection intents.
            if "INTO knowledge.sync_events" in compiled:
                return _CreateScriptedResult(
                    row=SimpleNamespace(
                        event_sequence=1,
                        committed_at=datetime(2026, 8, 23, 16, 0, 0, tzinfo=UTC),
                    )
                )
            return _CreateScriptedResult()
        if visit_name == "update":
            # The guarded active-pointer transition matches exactly one row.
            return _CreateScriptedResult(rowcount=1)
        raise AssertionError(f"unexpected statement kind: {visit_name}")


class _CreateTransitionStore(PostgresqlSourcePublicationStore):
    """Store double driving the real durable ``_create_transition``.

    The locked prefix's trusted-context seam is overridden exactly like
    :class:`_ControlledStore` (active workspace, valid actor, replay miss); a
    create builds its policy subject without database access, so everything
    from :meth:`_create_transition` onward runs as the durable code over the
    scripted connection. With ``with_operation_fence`` the small-file operation
    fence wraps the transition exactly as the receive path wires it.
    """

    def __init__(
        self,
        command: CreateSourceVersion,
        receipt: VerifiedObjectReceipt,
        *,
        foreign_active_locator_source_id: UUID | None,
        locator_insert_violates: bool,
        sources_insert_violates: bool = False,
        with_operation_fence: bool = False,
    ) -> None:
        self.connection = _CreateScriptedConnection(
            receipt,
            foreign_active_locator_source_id=foreign_active_locator_source_id,
            locator_insert_violates=locator_insert_violates,
            sources_insert_violates=sources_insert_violates,
        )
        self.order: list[str] = []
        fence = _RecordingOperationFence(self.order) if with_operation_fence else None
        super().__init__(
            cast(Any, _Engine(self.connection)),
            policy_verifier=_AcceptingVerifier(),
            small_file_operation_store=cast(Any, fence) if fence is not None else None,
            small_file_bound_operation=cast(Any, object()) if fence is not None else None,
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


def _allow_locked_publication_policy(monkeypatch: pytest.MonkeyPatch, workspace_id: UUID) -> None:
    async def authorize_locked(*args: object, **kwargs: object) -> AllowedPolicyRevisionBinding:
        del args, kwargs
        return AllowedPolicyRevisionBinding(workspace_id, 7)

    monkeypatch.setattr(
        publication_store,
        "authorize_locked_publication_policy",
        authorize_locked,
        raising=False,
    )


async def _commit_create(
    store: _CreateTransitionStore, command: CreateSourceVersion
) -> SourceVersionPublicationResult:
    receipt = build_verified_receipt(command.expected_object, _COMMIT_VERIFIED_AT)
    return await store.commit_create(
        command,
        compute_request_fingerprint(command),
        receipt,
        build_diagnostic_context(),
    )


@pytest.mark.asyncio
async def test_create_at_foreign_active_locator_rejects_typed_before_the_locator_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create at a foreign ACTIVE locator path is the typed locator conflict.

    The live stuck event (2026-08-23): the bound path already has an ACTIVE
    ``source_locators`` row owned by a different source, so the initial-locator
    INSERT deterministically violates the partial unique index
    ``uq_source_locators_active_workspace_path``. The durable transition must
    reject with the typed, non-retryable ``source_locator_conflict`` under the
    same locks, BEFORE the INSERT — never let the constraint violation escape
    as the retryable ``source_commit_outcome_unknown`` loop that leaves the
    bound operation row fenced in ``receiving`` forever.
    """

    command = build_create_command(initial_locator=NormalizedLocator("notes/conflicted.md"))
    _allow_locked_publication_policy(monkeypatch, command.workspace_id)
    store = _CreateTransitionStore(
        command,
        build_verified_receipt(command.expected_object, _COMMIT_VERIFIED_AT),
        foreign_active_locator_source_id=uuid4(),
        locator_insert_violates=True,
        with_operation_fence=True,
    )

    with pytest.raises(SourcePublicationError) as raised:
        await _commit_create(store, command)

    assert raised.value.error_code is ErrorCode.SOURCE_LOCATOR_CONFLICT
    assert raised.value.is_retryable is False
    assert raised.value.category is ErrorCategory.CONFLICT
    # The guarded pre-check fired inside the locked transition: the locator
    # INSERT (and its unique violation) is never reached.
    assert store.connection.locator_prechecks == 1
    assert store.connection.locator_inserts == 0
    # The standalone rejection audit carries the closed reason token.
    assert store.connection.audit_rows == [
        ("source.version_publish_rejected", "source_locator_conflict")
    ]
    # The bound operation row follows the existing typed-rejection pattern: the
    # fence is acquired, no terminal result is recorded, and no new state is
    # invented — the row stays ``receiving`` for the client's typed 409.
    assert store.order == ["operation_fence"]


@pytest.mark.asyncio
async def test_create_at_free_locator_path_publishes_the_bound_locator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely free bound path still publishes through the locator INSERT."""

    command = build_create_command(initial_locator=NormalizedLocator("notes/free.md"))
    _allow_locked_publication_policy(monkeypatch, command.workspace_id)
    store = _CreateTransitionStore(
        command,
        build_verified_receipt(command.expected_object, _COMMIT_VERIFIED_AT),
        foreign_active_locator_source_id=None,
        locator_insert_violates=False,
    )

    result = await _commit_create(store, command)

    assert result.outcome is PublicationOutcome.PUBLISHED
    assert result.source_id == command.source_id
    assert result.content_version == 1
    assert store.connection.locator_prechecks == 1
    assert store.connection.locator_inserts == 1
    # Only the in-transaction success audit exists; no rejection was written.
    assert store.connection.audit_rows == [("source.version_published", None)]


@pytest.mark.asyncio
async def test_locator_free_create_skips_the_locator_guard_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create without a bound initial locator never consults the locator table."""

    command = build_create_command()
    assert command.initial_locator is None
    _allow_locked_publication_policy(monkeypatch, command.workspace_id)
    store = _CreateTransitionStore(
        command,
        build_verified_receipt(command.expected_object, _COMMIT_VERIFIED_AT),
        foreign_active_locator_source_id=uuid4(),
        locator_insert_violates=True,
    )

    result = await _commit_create(store, command)

    assert result.outcome is PublicationOutcome.PUBLISHED
    assert store.connection.locator_prechecks == 0
    assert store.connection.locator_inserts == 0


@pytest.mark.asyncio
async def test_foreign_unique_violation_is_redacted_never_a_locator_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the active-locator pre-check is translated; other 23505s are not.

    A unique violation from a different constraint in the same create
    transaction must keep crossing the boundary as the redacted non-retryable
    ``internal_error`` of the narrowed classification contract in
    ``error_mapping`` (never the retryable
    ``source_commit_outcome_unknown`` and never the typed conflict): the
    typed conflict is scoped strictly to the guarded locator pre-check,
    never to SQLSTATE 23505 at large.
    """

    command = build_create_command(initial_locator=NormalizedLocator("notes/owner-clash.md"))
    _allow_locked_publication_policy(monkeypatch, command.workspace_id)
    store = _CreateTransitionStore(
        command,
        build_verified_receipt(command.expected_object, _COMMIT_VERIFIED_AT),
        foreign_active_locator_source_id=uuid4(),
        locator_insert_violates=True,
        sources_insert_violates=True,
    )

    with pytest.raises(InternalApplicationError) as raised:
        await _commit_create(store, command)

    assert raised.value.error_code is ErrorCode.INTERNAL_ERROR
    assert raised.value.is_retryable is False


_MISSING: Final[object] = object()
