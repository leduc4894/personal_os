"""Backend enforcement unit tests: verified snapshots, decisions, boundaries.

Proves the domain half of spec 14: the verify-and-parse path turns persisted
signed bytes plus their trust anchor back into the immutable revision and
fails closed on every corruption shape with the typed signing-unavailable
error; :class:`PolicyEnforcementService` maps a missing active policy, a
definite match and a raw indeterminate outcome onto the typed denial codes
with only their registered safe details; the publication and read boundary
subjects are built from the command's own evidence (an update resolves the
stored source type through the evidence port); and evaluation metrics carry
only the closed ``boundary`` and ``decision`` labels.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tests.unit.exclusion_policy.fakes import rule

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    ExclusionPolicyRevision,
    PolicySubject,
    PolicySubjectField,
    RawPolicyDecision,
    RuleKind,
)
from personal_os.exclusion_policy.enforcement import (
    REASON_REQUIRED_EVIDENCE_MISSING,
    ActivePolicySnapshotMaterial,
    AllowedPolicyRevisionBinding,
    KeyedTrustAnchorVerifier,
    PolicyDecision,
    PolicyEnforcementService,
    enforce_policy_decision,
    evaluate_policy_decision,
    parse_verified_policy_revision,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.metrics import (
    EvaluationMetricOutcome,
    InMemoryExclusionPolicyMetrics,
    PolicyBoundary,
)
from personal_os.exclusion_policy.signatures import (
    SNAPSHOT_SIGNING_DOMAIN,
    build_signed_message,
    build_snapshot_payload,
    compute_payload_sha256_hex,
)
from personal_os.object_storage import CanonicalMediaType
from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    SourceTitle,
    SourceType,
    UpdateSourceVersion,
)
from personal_os.sources.errors import SourcePublicationError

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-000000000001")
POLICY_REVISION_ID = UUID("018f47a0-7b00-7000-8000-0000000000e1")
PUBLISHED_AT = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)

_TRUST_ANCHOR = bytes(range(32))
_SIGNATURE = bytes(range(64))


class AcceptingTrustVerifier:
    """Trust-anchor verification fake accepting exactly the pinned geometry."""

    def verify(self, *, public_key_bytes: bytes, signature_bytes: bytes, message: bytes) -> bool:
        return (
            len(public_key_bytes) == 32
            and len(signature_bytes) == 64
            and public_key_bytes == _TRUST_ANCHOR
        )


class RejectingTrustVerifier:
    """Trust-anchor verification fake failing every verification."""

    def verify(self, *, public_key_bytes: bytes, signature_bytes: bytes, message: bytes) -> bool:
        return False


class ScriptedSnapshotSource:
    """Active-snapshot port fake returning one scripted material or ``None``."""

    def __init__(self, material: ActivePolicySnapshotMaterial | None) -> None:
        self.material = material
        self.requested_workspaces: list[UUID] = []

    async def load_active_snapshot(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> ActivePolicySnapshotMaterial | None:
        self.requested_workspaces.append(workspace_id)
        return self.material


class ScriptedEvidenceSource:
    """Subject-evidence port fake returning one scripted subject or ``None``."""

    def __init__(self, subject: PolicySubject | None) -> None:
        self.subject = subject
        self.requested_sources: list[UUID] = []

    async def load_subject_evidence(
        self, workspace_id: UUID, source_id: UUID, context: DiagnosticContext
    ) -> PolicySubject | None:
        self.requested_sources.append(source_id)
        return self.subject


def build_material(
    revision: ExclusionPolicyRevision,
    *,
    payload_bytes: bytes | None = None,
    payload_sha256: str | None = None,
    signature_bytes: bytes = _SIGNATURE,
) -> ActivePolicySnapshotMaterial:
    """One signed snapshot material over the revision, with overridable parts."""

    canonical_payload = (
        payload_bytes
        if payload_bytes is not None
        else build_snapshot_payload(
            revision, parent_policy_revision_id=None, published_at=PUBLISHED_AT
        )
    )
    return ActivePolicySnapshotMaterial(
        workspace_id=revision.workspace_id,
        policy_revision_id=revision.policy_revision_id,
        revision_number=revision.revision_number,
        payload_bytes=canonical_payload,
        payload_sha256=payload_sha256
        if payload_sha256 is not None
        else compute_payload_sha256_hex(canonical_payload),
        signature_bytes=signature_bytes,
        public_key_bytes=_TRUST_ANCHOR,
    )


def build_revision(*rules) -> ExclusionPolicyRevision:
    return ExclusionPolicyRevision(
        policy_revision_id=POLICY_REVISION_ID,
        workspace_id=WORKSPACE_ID,
        revision_number=1,
        rules=rules,
    )


def build_service(
    *,
    material: ActivePolicySnapshotMaterial | None,
    evidence: PolicySubject | None = None,
    verifier: object | None = None,
    metrics: InMemoryExclusionPolicyMetrics | None = None,
) -> tuple[
    PolicyEnforcementService,
    ScriptedSnapshotSource,
    ScriptedEvidenceSource,
    InMemoryExclusionPolicyMetrics,
]:
    snapshot_source = ScriptedSnapshotSource(material)
    evidence_source = ScriptedEvidenceSource(evidence)
    bound_metrics = metrics if metrics is not None else InMemoryExclusionPolicyMetrics()
    service = PolicyEnforcementService(
        snapshot_source=snapshot_source,
        evidence_source=evidence_source,
        verifier=verifier if verifier is not None else AcceptingTrustVerifier(),
        metrics=bound_metrics,
        clock=lambda: PUBLISHED_AT,
    )
    return service, snapshot_source, evidence_source, bound_metrics


def build_create_command(
    *,
    source_type: SourceType = SourceType.MARKDOWN,
    media_type: str = "text/markdown",
    size_bytes: int = 25,
) -> CreateSourceVersion:
    from hashlib import sha256

    from personal_os.object_storage import ContentDigest, ExpectedObject

    payload = b"canonical enforcement bytes"
    return CreateSourceVersion(
        workspace_id=WORKSPACE_ID,
        source_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey("enforcement-create-001"),
        source_type=source_type,
        title=SourceTitle("Enforcement Subject"),
        actor=SourceActor(actor_kind=ActorKind.USER, actor_id=uuid4()),
        expected_object=ExpectedObject(
            content_digest=ContentDigest.parse(sha256(payload).hexdigest()),
            size_bytes=size_bytes,
            media_type=CanonicalMediaType.parse(media_type),
        ),
        client_timestamp=None,
    )


def context() -> DiagnosticContext:
    return create_diagnostic_context().context


# --- signed-snapshot verification -------------------------------------------------


def test_verified_snapshot_round_trips_into_the_immutable_revision() -> None:
    revision = build_revision(
        rule(RuleKind.MAXIMUM_SIZE, size_bytes_operand=1024),
        rule(RuleKind.MEDIA_TYPE, text_operand="text/markdown"),
    )
    material = build_material(revision)

    parsed = parse_verified_policy_revision(material, verifier=AcceptingTrustVerifier())

    assert parsed.policy_revision_id == revision.policy_revision_id
    assert parsed.workspace_id == WORKSPACE_ID
    assert parsed.revision_number == 1
    # The payload orders rules by textual rule_id; the rule SET round-trips.
    assert sorted(parsed.rules, key=lambda signed: signed.rule_id) == sorted(
        revision.rules, key=lambda signed: signed.rule_id
    )


def test_corrupt_signature_material_fails_closed_as_signing_unavailable() -> None:
    revision = build_revision()
    for corrupted in (
        build_material(revision, payload_sha256="0" * 64),
        build_material(revision, payload_bytes=b"not-json"),
    ):
        with pytest.raises(ExclusionPolicyError) as raised:
            parse_verified_policy_revision(corrupted, verifier=AcceptingTrustVerifier())
        assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
        assert raised.value.safe_details == {}


def test_snapshot_material_geometry_rejects_wrong_signature_length() -> None:
    revision = build_revision()
    with pytest.raises(ValueError, match="signature_bytes"):
        _ = build_material(revision, signature_bytes=bytes(63))


def test_failed_signature_verification_fails_closed() -> None:
    material = build_material(build_revision())
    with pytest.raises(ExclusionPolicyError) as raised:
        parse_verified_policy_revision(material, verifier=RejectingTrustVerifier())
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE


def test_tampered_payload_fails_signature_verification() -> None:
    import json

    revision = build_revision(rule(RuleKind.MAXIMUM_SIZE, size_bytes_operand=1024))
    material = build_material(revision)
    tampered_document = json.loads(material.payload_bytes)
    tampered_document["rules"][0]["maximum_size_bytes"] = 2048
    tampered_bytes = json.dumps(tampered_document, sort_keys=True, separators=(",", ":")).encode()
    tampered = build_material(
        revision,
        payload_bytes=tampered_bytes,
        payload_sha256=compute_payload_sha256_hex(tampered_bytes),
    )

    class SingleMessageVerifier:
        """Accepts only the message of the untampered original payload."""

        def __init__(self) -> None:
            self.expected = build_signed_message(SNAPSHOT_SIGNING_DOMAIN, material.payload_bytes)

        def verify(
            self, *, public_key_bytes: bytes, signature_bytes: bytes, message: bytes
        ) -> bool:
            return message == self.expected

    with pytest.raises(ExclusionPolicyError) as raised:
        parse_verified_policy_revision(tampered, verifier=SingleMessageVerifier())
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE


def test_payload_identity_drift_fails_closed() -> None:
    revision = build_revision()
    material = ActivePolicySnapshotMaterial(
        workspace_id=WORKSPACE_ID,
        # The payload names the workspace and revision of the material; a
        # diverging row identity is corruption.
        policy_revision_id=uuid4(),
        revision_number=1,
        payload_bytes=material_bytes(revision),
        payload_sha256=compute_payload_sha256_hex(material_bytes(revision)),
        signature_bytes=_SIGNATURE,
        public_key_bytes=_TRUST_ANCHOR,
    )
    with pytest.raises(ExclusionPolicyError) as raised:
        parse_verified_policy_revision(material, verifier=AcceptingTrustVerifier())
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE


def material_bytes(revision: ExclusionPolicyRevision) -> bytes:
    return build_snapshot_payload(
        revision, parent_policy_revision_id=None, published_at=PUBLISHED_AT
    )


# --- decision evidence ------------------------------------------------------------


def test_decision_construction_enforces_closed_geometry() -> None:
    allowed = build_allowed_decision()
    assert len(allowed.subject_fingerprint) == 32
    assert allowed.enforced_decision is EnforcedPolicyDecision.ALLOWED
    with pytest.raises(ValueError, match="subject_fingerprint"):
        _ = PolicyDecision(
            workspace_id=WORKSPACE_ID,
            policy_revision_id=POLICY_REVISION_ID,
            revision_number=1,
            subject_fingerprint=bytes(31),
            raw_decision=RawPolicyDecision.ALLOWED,
            enforced_decision=EnforcedPolicyDecision.ALLOWED,
            matched_rule_ids=(),
            missing_fields=(),
            evaluated_at=PUBLISHED_AT,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        _ = PolicyDecision(
            workspace_id=WORKSPACE_ID,
            policy_revision_id=POLICY_REVISION_ID,
            revision_number=1,
            subject_fingerprint=bytes(32),
            raw_decision=RawPolicyDecision.ALLOWED,
            enforced_decision=EnforcedPolicyDecision.ALLOWED,
            matched_rule_ids=(),
            missing_fields=(),
            evaluated_at=datetime(2026, 8, 17, 12, 0, 0),
        )


def test_allowed_policy_revision_binding_rejects_nil_workspace_and_non_positive_revision() -> None:
    valid = AllowedPolicyRevisionBinding(
        workspace_id=WORKSPACE_ID,
        policy_revision_number=1,
    )

    assert valid.workspace_id == WORKSPACE_ID
    assert valid.policy_revision_number == 1
    with pytest.raises(ValueError, match="workspace_id"):
        _ = AllowedPolicyRevisionBinding(
            workspace_id=UUID(int=0),
            policy_revision_number=1,
        )
    with pytest.raises(ValueError, match="policy_revision_number"):
        _ = AllowedPolicyRevisionBinding(
            workspace_id=WORKSPACE_ID,
            policy_revision_number=0,
        )
    with pytest.raises(ValueError, match="policy_revision_number"):
        _ = AllowedPolicyRevisionBinding(
            workspace_id=WORKSPACE_ID,
            policy_revision_number=-1,
        )


def build_allowed_decision() -> PolicyDecision:
    return evaluate_policy_decision(
        revision=build_revision(),
        subject=PolicySubject(workspace_id=WORKSPACE_ID, source_id=uuid4()),
        evaluated_at=PUBLISHED_AT,
    )


def test_enforce_policy_decision_maps_raw_outcomes_onto_typed_denials() -> None:
    allowed = build_allowed_decision()
    enforce_policy_decision(allowed)  # No denial for an allowed subject.

    excluded = PolicyDecision(
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        revision_number=3,
        subject_fingerprint=bytes(32),
        raw_decision=RawPolicyDecision.EXCLUDED,
        enforced_decision=EnforcedPolicyDecision.EXCLUDED,
        matched_rule_ids=(uuid4(),),
        missing_fields=(),
        evaluated_at=PUBLISHED_AT,
    )
    with pytest.raises(ExclusionPolicyError) as raised:
        enforce_policy_decision(excluded)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    assert raised.value.safe_details == {"policy_revision_number": 3}

    indeterminate = PolicyDecision(
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        revision_number=3,
        subject_fingerprint=bytes(32),
        raw_decision=RawPolicyDecision.INDETERMINATE,
        enforced_decision=EnforcedPolicyDecision.EXCLUDED,
        matched_rule_ids=(),
        missing_fields=(PolicySubjectField.NORMALIZED_LOCATOR,),
        evaluated_at=PUBLISHED_AT,
    )
    with pytest.raises(ExclusionPolicyError) as raised:
        enforce_policy_decision(indeterminate)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INDETERMINATE
    assert raised.value.safe_details == {"reason": REASON_REQUIRED_EVIDENCE_MISSING}


# --- preflight authorization ------------------------------------------------------


@pytest.mark.asyncio
async def test_authorize_preflight_returns_the_allowing_decision() -> None:
    material = build_material(build_revision())
    service, _, _, metrics = build_service(material=material)

    decision = await service.authorize_preflight(
        subject=PolicySubject(workspace_id=WORKSPACE_ID, source_id=uuid4()),
        boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
        context=context(),
    )

    assert decision.raw_decision is RawPolicyDecision.ALLOWED
    assert decision.revision_number == 1
    assert (
        metrics.evaluation_count(PolicyBoundary.SINGLE_PART_UPLOAD, EvaluationMetricOutcome.ALLOWED)
        == 1
    )


@pytest.mark.asyncio
async def test_authorize_preflight_denies_a_definite_match() -> None:
    revision = build_revision(rule(RuleKind.MEDIA_TYPE, text_operand="text/markdown"))
    service, _, _, metrics = build_service(material=build_material(revision))

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.authorize_preflight(
            subject=PolicySubject(
                workspace_id=WORKSPACE_ID,
                source_id=uuid4(),
                media_type=CanonicalMediaType.parse("text/markdown"),
            ),
            boundary=PolicyBoundary.CANONICAL_READ,
            context=context(),
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    assert raised.value.safe_details == {"policy_revision_number": 1}
    assert (
        metrics.evaluation_count(PolicyBoundary.CANONICAL_READ, EvaluationMetricOutcome.EXCLUDED)
        == 1
    )


@pytest.mark.asyncio
async def test_authorize_preflight_denies_indeterminate_with_closed_reason() -> None:
    revision = build_revision(rule(RuleKind.EXTENSION, text_operand=".md"))
    service, _, _, _ = build_service(material=build_material(revision))

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.authorize_preflight(
            subject=PolicySubject(workspace_id=WORKSPACE_ID, source_id=uuid4()),
            boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
            context=context(),
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INDETERMINATE
    assert raised.value.safe_details == {"reason": REASON_REQUIRED_EVIDENCE_MISSING}


@pytest.mark.asyncio
async def test_authorize_preflight_denies_when_no_active_policy_exists() -> None:
    service, snapshot_source, _, _ = build_service(material=None)

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.authorize_preflight(
            subject=PolicySubject(workspace_id=WORKSPACE_ID, source_id=uuid4()),
            boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
            context=context(),
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED
    assert snapshot_source.requested_workspaces == [WORKSPACE_ID]


@pytest.mark.asyncio
async def test_authorize_preflight_denies_on_corrupt_signature_material() -> None:
    material = build_material(build_revision(), payload_sha256="1" * 64)
    service, _, _, _ = build_service(material=material)

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.authorize_preflight(
            subject=PolicySubject(workspace_id=WORKSPACE_ID, source_id=uuid4()),
            boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
            context=context(),
        )
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE


@pytest.mark.asyncio
async def test_authorize_publication_builds_the_create_subject_from_the_command() -> None:
    revision = build_revision(rule(RuleKind.SOURCE_TYPE, text_operand="pdf"))
    service, _, _, _ = build_service(material=build_material(revision))
    command = build_create_command()

    decision = await service.authorize_publication(command, context())

    # The markdown create is allowed under a pdf-only exclusion.
    assert decision.raw_decision is RawPolicyDecision.ALLOWED

    pdf_command = build_create_command(source_type=SourceType.PDF)
    with pytest.raises(ExclusionPolicyError) as raised:
        await service.authorize_publication(pdf_command, context())
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED


@pytest.mark.asyncio
async def test_authorize_publication_resolves_update_type_evidence() -> None:
    revision = build_revision(rule(RuleKind.SOURCE_TYPE, text_operand="pdf"))
    source_id = uuid4()
    evidence = PolicySubject(
        workspace_id=WORKSPACE_ID, source_id=source_id, source_type=SourceType.PDF
    )
    service, _, evidence_source, _ = build_service(
        material=build_material(revision), evidence=evidence
    )
    from hashlib import sha256

    from personal_os.object_storage import ContentDigest, ExpectedObject

    update_command = UpdateSourceVersion(
        workspace_id=WORKSPACE_ID,
        source_id=source_id,
        event_id=uuid4(),
        idempotency_key=IdempotencyKey("enforcement-update-001"),
        base_version_id=uuid4(),
        actor=SourceActor(actor_kind=ActorKind.DEVICE, actor_id=uuid4()),
        expected_object=ExpectedObject(
            content_digest=ContentDigest.parse(sha256(b"bytes").hexdigest()),
            size_bytes=5,
            media_type=CanonicalMediaType.parse("text/markdown"),
        ),
        client_timestamp=None,
    )

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.authorize_publication(update_command, context())
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    assert evidence_source.requested_sources == [source_id]


# --- bound publication authorization ---------------------------------------------


@pytest.mark.asyncio
async def test_bound_publication_returns_binding_without_evaluation_when_revision_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching signed revision preserves the preflight allow evidence."""

    service, snapshot_source, _, metrics = build_service(material=build_material(build_revision()))
    binding = AllowedPolicyRevisionBinding(workspace_id=WORKSPACE_ID, policy_revision_number=1)

    def evaluator_must_not_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("the equal-revision path must not evaluate")

    monkeypatch.setattr(
        "personal_os.exclusion_policy.enforcement.evaluate_policy", evaluator_must_not_run
    )

    evidence = await service.authorize_bound_publication(build_create_command(), binding, context())

    assert evidence is binding
    assert snapshot_source.requested_workspaces == [WORKSPACE_ID]
    assert len(metrics.evaluation_records()) == 1
    record = metrics.evaluation_records()[0]
    assert record.boundary is PolicyBoundary.SINGLE_PART_UPLOAD
    assert record.decision is EvaluationMetricOutcome.ALLOWED


@pytest.mark.asyncio
async def test_bound_publication_evaluates_the_current_revision_when_revision_changed() -> None:
    """A changed fully decidable policy may allow the publication."""

    current_revision = ExclusionPolicyRevision(
        policy_revision_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        revision_number=2,
        rules=(rule(RuleKind.MEDIA_TYPE, text_operand="application/pdf"),),
    )
    service, _, _, _ = build_service(material=build_material(current_revision))
    binding = AllowedPolicyRevisionBinding(workspace_id=WORKSPACE_ID, policy_revision_number=1)

    evidence = await service.authorize_bound_publication(
        build_create_command(media_type="text/markdown"), binding, context()
    )

    assert isinstance(evidence, PolicyDecision)
    assert evidence.revision_number == 2
    assert evidence.raw_decision is RawPolicyDecision.ALLOWED


@pytest.mark.asyncio
async def test_bound_publication_verifies_changed_revision_once() -> None:
    """A mismatch evaluates the already verified active revision exactly once."""

    class CountingTrustVerifier:
        def __init__(self) -> None:
            self.verify_calls = 0

        def verify(
            self, *, public_key_bytes: bytes, signature_bytes: bytes, message: bytes
        ) -> bool:
            self.verify_calls += 1
            return True

    current_revision = ExclusionPolicyRevision(
        policy_revision_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        revision_number=2,
        rules=(),
    )
    verifier = CountingTrustVerifier()
    service, _, _, _ = build_service(material=build_material(current_revision), verifier=verifier)

    evidence = await service.authorize_bound_publication(
        build_create_command(),
        AllowedPolicyRevisionBinding(workspace_id=WORKSPACE_ID, policy_revision_number=1),
        context(),
    )

    assert isinstance(evidence, PolicyDecision)
    assert verifier.verify_calls == 1


@pytest.mark.asyncio
async def test_bound_publication_denies_changed_locator_rule_as_indeterminate() -> None:
    """A changed locator-dependent rule fails closed without locator evidence."""

    current_revision = ExclusionPolicyRevision(
        policy_revision_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        revision_number=2,
        rules=(rule(RuleKind.EXTENSION, text_operand=".md"),),
    )
    service, _, _, _ = build_service(material=build_material(current_revision))

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.authorize_bound_publication(
            build_create_command(),
            AllowedPolicyRevisionBinding(workspace_id=WORKSPACE_ID, policy_revision_number=1),
            context(),
        )

    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INDETERMINATE


@pytest.mark.asyncio
async def test_bound_publication_rejects_foreign_workspace_binding() -> None:
    """A binding never authorizes a command from another workspace."""

    service, snapshot_source, _, _ = build_service(material=build_material(build_revision()))

    with pytest.raises(SourcePublicationError) as raised:
        await service.authorize_bound_publication(
            build_create_command(),
            AllowedPolicyRevisionBinding(workspace_id=uuid4(), policy_revision_number=1),
            context(),
        )

    assert raised.value.error_code is ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED
    assert snapshot_source.requested_workspaces == []


@pytest.mark.asyncio
async def test_bound_publication_fails_closed_when_active_snapshot_is_missing_or_invalid() -> None:
    """Missing and corrupt current snapshots deny before any publication subject load."""

    for material, expected_error in (
        (None, ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED),
        (
            build_material(build_revision(), payload_sha256="0" * 64),
            ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE,
        ),
    ):
        service, _, evidence_source, _ = build_service(material=material)

        with pytest.raises(ExclusionPolicyError) as raised:
            await service.authorize_bound_publication(
                build_create_command(),
                AllowedPolicyRevisionBinding(workspace_id=WORKSPACE_ID, policy_revision_number=1),
                context(),
            )

        assert raised.value.error_code is expected_error
        assert evidence_source.requested_sources == []


# --- fail-closed system failures record the closed outcome (G1) --------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    [PolicyBoundary.SINGLE_PART_UPLOAD, PolicyBoundary.CANONICAL_READ],
)
async def test_missing_active_policy_records_the_failed_evaluation_with_the_closed_code(
    boundary: PolicyBoundary,
) -> None:
    """The not-initialized raise records the closed failed outcome, not nothing."""

    service, _, _, metrics = build_service(material=None)

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.authorize_preflight(
            subject=PolicySubject(workspace_id=WORKSPACE_ID, source_id=uuid4()),
            boundary=boundary,
            context=context(),
        )

    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED
    assert metrics.evaluation_count(boundary, EvaluationMetricOutcome.FAILED) == 1
    (record,) = metrics.evaluation_records()
    assert record.boundary is boundary
    assert record.decision is EvaluationMetricOutcome.FAILED
    assert record.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    [PolicyBoundary.SINGLE_PART_UPLOAD, PolicyBoundary.CANONICAL_READ],
)
async def test_corrupt_signing_material_records_the_failed_evaluation_with_the_closed_code(
    boundary: PolicyBoundary,
) -> None:
    """The signing-unavailable raise records the closed failed outcome, not nothing."""

    material = build_material(build_revision(), payload_sha256="1" * 64)
    service, _, _, metrics = build_service(material=material)

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.authorize_preflight(
            subject=PolicySubject(workspace_id=WORKSPACE_ID, source_id=uuid4()),
            boundary=boundary,
            context=context(),
        )

    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
    assert metrics.evaluation_count(boundary, EvaluationMetricOutcome.FAILED) == 1
    (record,) = metrics.evaluation_records()
    assert record.boundary is boundary
    assert record.decision is EvaluationMetricOutcome.FAILED
    assert record.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE


@pytest.mark.asyncio
async def test_bound_publication_fail_closed_raises_record_the_failed_outcome() -> None:
    """Both bound-publication system failures record failed with their code."""

    for material, expected_error in (
        (None, ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED),
        (
            build_material(build_revision(), payload_sha256="0" * 64),
            ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE,
        ),
    ):
        service, _, _, metrics = build_service(material=material)

        with pytest.raises(ExclusionPolicyError) as raised:
            await service.authorize_bound_publication(
                build_create_command(),
                AllowedPolicyRevisionBinding(workspace_id=WORKSPACE_ID, policy_revision_number=1),
                context(),
            )

        assert raised.value.error_code is expected_error
        assert (
            metrics.evaluation_count(
                PolicyBoundary.SINGLE_PART_UPLOAD, EvaluationMetricOutcome.FAILED
            )
            == 1
        )
        (record,) = metrics.evaluation_records()
        assert record.decision is EvaluationMetricOutcome.FAILED
        assert record.error_code is expected_error


@pytest.mark.asyncio
async def test_policy_denials_keep_their_closed_outcomes_unrecorded_as_failed() -> None:
    """Denial semantics stay unchanged: excluded/indeterminate, never failed."""

    denied_revision = build_revision(rule(RuleKind.MEDIA_TYPE, text_operand="text/markdown"))
    service, _, _, metrics = build_service(material=build_material(denied_revision))

    with pytest.raises(ExclusionPolicyError):
        await service.authorize_preflight(
            subject=PolicySubject(
                workspace_id=WORKSPACE_ID,
                source_id=uuid4(),
                media_type=CanonicalMediaType.parse("text/markdown"),
            ),
            boundary=PolicyBoundary.CANONICAL_READ,
            context=context(),
        )

    assert (
        metrics.evaluation_count(PolicyBoundary.CANONICAL_READ, EvaluationMetricOutcome.EXCLUDED)
        == 1
    )
    assert (
        metrics.evaluation_count(PolicyBoundary.CANONICAL_READ, EvaluationMetricOutcome.FAILED) == 0
    )


@pytest.mark.asyncio
async def test_keyed_trust_anchor_verifier_adapts_the_keyed_port() -> None:
    material = build_material(build_revision())
    message = build_signed_message(SNAPSHOT_SIGNING_DOMAIN, material.payload_bytes)
    from personal_os.exclusion_policy.signatures import derive_ed25519_key_id

    class RecordingKeyedVerifier:
        def __init__(self) -> None:
            self.key_ids: list[str] = []

        def verify(self, key_id: str, signature: bytes, message: bytes) -> bool:
            self.key_ids.append(key_id)
            return True

    keyed = RecordingKeyedVerifier()
    adapter = KeyedTrustAnchorVerifier(keyed_verifier=keyed)

    assert adapter.verify(
        public_key_bytes=material.public_key_bytes,
        signature_bytes=material.signature_bytes,
        message=message,
    )
    assert keyed.key_ids == [derive_ed25519_key_id(material.public_key_bytes)]
