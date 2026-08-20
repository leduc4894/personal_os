"""Bound policy evidence at the transaction-final PostgreSQL lock."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.unit.sources.fakes import build_policy_decision

from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    ExclusionPolicyRevision,
    PolicySubject,
    RawPolicyDecision,
)
from personal_os.exclusion_policy.enforcement import (
    ActivePolicySnapshotMaterial,
    AllowedPolicyRevisionBinding,
    PolicyDecision,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.metrics import (
    EvaluationMetricOutcome,
    InMemoryExclusionPolicyMetrics,
    PolicyBoundary,
)
from personal_os.exclusion_policy.signatures import (
    build_snapshot_payload,
    compute_payload_sha256_hex,
)
from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    SourceTitle,
    SourceType,
)
from personal_os.sources.errors import SourcePublicationError
from postgresql_source_store import policy_enforcement

_WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-000000000001")
_POLICY_REVISION_ID = UUID("018f47a0-7b00-7000-8000-0000000000e1")
_PUBLISHED_AT = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
_TRUST_ANCHOR = bytes(range(32))
_SIGNATURE = bytes(range(64))


class _AcceptingVerifier:
    def verify(
        self,
        *,
        public_key_bytes: bytes,
        signature_bytes: bytes,
        message: bytes,
    ) -> bool:
        del message
        return public_key_bytes == _TRUST_ANCHOR and signature_bytes == _SIGNATURE


class _RejectingVerifier:
    def verify(
        self,
        *,
        public_key_bytes: bytes,
        signature_bytes: bytes,
        message: bytes,
    ) -> bool:
        del public_key_bytes, signature_bytes, message
        return False


class _StateResult:
    def __init__(self, active_policy_revision_id: UUID | None, *, has_row: bool = True) -> None:
        self._active_policy_revision_id = active_policy_revision_id
        self._has_row = has_row

    def one_or_none(self) -> object | None:
        if not self._has_row:
            return None
        return SimpleNamespace(active_policy_revision_id=self._active_policy_revision_id)


class _SnapshotResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> _SnapshotResult:
        return self

    def first(self) -> dict[str, object] | None:
        return self._row


class _ScriptedConnection:
    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.executed_statements: list[object] = []

    async def execute(self, statement: object) -> object:
        self.executed_statements.append(statement)
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _command() -> CreateSourceVersion:
    return CreateSourceVersion(
        workspace_id=_WORKSPACE_ID,
        source_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey("locked-policy-unit-1"),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Locked policy subject"),
        actor=SourceActor(ActorKind.USER, uuid4()),
        expected_object=ExpectedObject(
            content_digest=ContentDigest.parse("a" * 64),
            size_bytes=41,
            media_type=CanonicalMediaType.parse("text/markdown"),
        ),
        client_timestamp=None,
    )


def _subject(command: CreateSourceVersion) -> PolicySubject:
    return PolicySubject(
        workspace_id=command.workspace_id,
        source_id=command.source_id,
        source_type=command.source_type,
        media_type=command.expected_object.media_type,
        size_bytes=command.expected_object.size_bytes,
    )


def _material(revision_number: int) -> ActivePolicySnapshotMaterial:
    revision = ExclusionPolicyRevision(
        policy_revision_id=_POLICY_REVISION_ID,
        workspace_id=_WORKSPACE_ID,
        revision_number=revision_number,
        rules=(),
    )
    payload_bytes = build_snapshot_payload(
        revision,
        parent_policy_revision_id=None,
        published_at=_PUBLISHED_AT,
    )
    return ActivePolicySnapshotMaterial(
        workspace_id=revision.workspace_id,
        policy_revision_id=revision.policy_revision_id,
        revision_number=revision.revision_number,
        payload_bytes=payload_bytes,
        payload_sha256=compute_payload_sha256_hex(payload_bytes),
        signature_bytes=_SIGNATURE,
        public_key_bytes=_TRUST_ANCHOR,
    )


def _snapshot_row(material: ActivePolicySnapshotMaterial) -> dict[str, object]:
    return {
        "workspace_id": material.workspace_id,
        "policy_revision_id": material.policy_revision_id,
        "revision_number": material.revision_number,
        "payload_bytes": material.payload_bytes,
        "payload_sha256": material.payload_sha256,
        "signature_bytes": material.signature_bytes,
        "public_key_bytes": material.public_key_bytes,
    }


def _connection_for(material: ActivePolicySnapshotMaterial) -> _ScriptedConnection:
    return _ScriptedConnection(
        _StateResult(material.policy_revision_id),
        _SnapshotResult(_snapshot_row(material)),
    )


def _allowed_decision(command: CreateSourceVersion, revision_number: int) -> PolicyDecision:
    return PolicyDecision(
        workspace_id=command.workspace_id,
        policy_revision_id=_POLICY_REVISION_ID,
        revision_number=revision_number,
        subject_fingerprint=bytes(range(32)),
        raw_decision=RawPolicyDecision.ALLOWED,
        enforced_decision=EnforcedPolicyDecision.ALLOWED,
        matched_rule_ids=(),
        missing_fields=(),
        evaluated_at=_PUBLISHED_AT,
    )


@pytest.mark.asyncio
async def test_matching_binding_returns_verified_binding_without_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command()
    subject = _subject(command)
    material = _material(7)
    connection = _connection_for(material)
    binding = AllowedPolicyRevisionBinding(command.workspace_id, 7)
    metrics = InMemoryExclusionPolicyMetrics()

    def unexpected_evaluation(**kwargs: Any) -> PolicyDecision:
        pytest.fail(f"equal verified binding invoked evaluator: {tuple(kwargs)}")

    monkeypatch.setattr(policy_enforcement, "evaluate_policy_decision", unexpected_evaluation)

    result = await policy_enforcement.authorize_locked_publication_policy(
        connection,
        command,
        subject,
        binding,
        _AcceptingVerifier(),
        metrics,
    )

    assert result is binding
    assert len(connection.executed_statements) == 2
    assert "FOR UPDATE" in str(connection.executed_statements[0])
    assert (
        metrics.evaluation_count(
            PolicyBoundary.SOURCE_CREATE_UPDATE,
            EvaluationMetricOutcome.ALLOWED,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_changed_binding_revision_evaluates_supplied_authoritative_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command()
    subject = _subject(command)
    connection = _connection_for(_material(8))
    binding = AllowedPolicyRevisionBinding(command.workspace_id, 7)
    evaluated_subjects: list[PolicySubject] = []
    current_decision = _allowed_decision(command, 8)

    def capture_evaluation(**kwargs: Any) -> PolicyDecision:
        evaluated_subjects.append(kwargs["subject"])
        return current_decision

    monkeypatch.setattr(policy_enforcement, "evaluate_policy_decision", capture_evaluation)

    result = await policy_enforcement.authorize_locked_publication_policy(
        connection,
        command,
        subject,
        binding,
        _AcceptingVerifier(),
        None,
    )

    assert result is current_decision
    assert evaluated_subjects == [subject]
    assert evaluated_subjects[0] is subject


@pytest.mark.asyncio
async def test_ordinary_policy_decision_evaluates_even_when_revision_numbers_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command()
    subject = _subject(command)
    connection = _connection_for(_material(7))
    preflight_decision = build_policy_decision(
        workspace_id=command.workspace_id,
        revision_number=7,
    )
    locked_decision = _allowed_decision(command, 7)
    evaluated_subjects: list[PolicySubject] = []

    def capture_evaluation(**kwargs: Any) -> PolicyDecision:
        evaluated_subjects.append(kwargs["subject"])
        return locked_decision

    monkeypatch.setattr(policy_enforcement, "evaluate_policy_decision", capture_evaluation)

    result = await policy_enforcement.authorize_locked_publication_policy(
        connection,
        command,
        subject,
        preflight_decision,
        _AcceptingVerifier(),
        None,
    )

    assert result is locked_decision
    assert result is not preflight_decision
    assert evaluated_subjects == [subject]


@pytest.mark.asyncio
async def test_locked_authorization_fails_closed_without_an_active_snapshot() -> None:
    command = _command()
    connection = _ScriptedConnection(_StateResult(None, has_row=False))

    with pytest.raises(ExclusionPolicyError) as raised:
        await policy_enforcement.authorize_locked_publication_policy(
            connection,
            command,
            _subject(command),
            AllowedPolicyRevisionBinding(command.workspace_id, 7),
            _AcceptingVerifier(),
            None,
        )

    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED


@pytest.mark.asyncio
async def test_locked_authorization_fails_closed_on_invalid_signature() -> None:
    command = _command()

    with pytest.raises(ExclusionPolicyError) as raised:
        await policy_enforcement.authorize_locked_publication_policy(
            _connection_for(_material(7)),
            command,
            _subject(command),
            AllowedPolicyRevisionBinding(command.workspace_id, 7),
            _RejectingVerifier(),
            None,
        )

    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE


@pytest.mark.asyncio
async def test_locked_authorization_fails_closed_on_connection_failure() -> None:
    command = _command()

    with pytest.raises(RuntimeError, match="database unavailable"):
        await policy_enforcement.authorize_locked_publication_policy(
            _ScriptedConnection(RuntimeError("database unavailable")),
            command,
            _subject(command),
            AllowedPolicyRevisionBinding(command.workspace_id, 7),
            _AcceptingVerifier(),
            None,
        )


@pytest.mark.asyncio
async def test_locked_authorization_rejects_foreign_workspace_binding() -> None:
    command = _command()

    with pytest.raises(SourcePublicationError) as raised:
        await policy_enforcement.authorize_locked_publication_policy(
            _connection_for(_material(7)),
            command,
            _subject(command),
            AllowedPolicyRevisionBinding(uuid4(), 7),
            _AcceptingVerifier(),
            None,
        )

    assert raised.value.error_code is ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED
    assert raised.value.safe_details == {"source_id": command.source_id}
