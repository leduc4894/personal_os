"""Mandatory backend enforcement of the active signed exclusion policy.

This module owns the provider-neutral half of spec section 14: the
internal-only :class:`PolicyDecision` evidence value, the persisted active
snapshot material shape, the verify-and-parse path that turns persisted
snapshot bytes plus their trust anchor back into an immutable
:class:`~personal_os.exclusion_policy.contracts.ExclusionPolicyRevision`, and
the :class:`PolicyEnforcementService` application service whose
``authorize_preflight`` evaluates one candidate subject against the currently
active revision before any object-store access. Every fail-closed rule of the
spec maps to a typed error: a missing active signed policy is the typed
not-initialized denial, corrupt signature material is the typed
signing-unavailable denial, a definite match is the typed denied error
carrying only the revision number, and a raw indeterminate outcome is the
typed indeterminate error carrying only the closed reason token. The service
never falls back to a projection, plugin decision or cached client claim, and
metrics record only the closed ``boundary`` and ``decision`` labels.

Like the rest of the package this module imports no web framework, database
driver, provider SDK or cryptography library: verification runs through the
pinned trust-anchor verification port (adapting the closed keyed
:class:`~personal_os.exclusion_policy.signatures.PolicySignatureVerifier` when
the composition binds one), and the PostgreSQL half — snapshot loading,
subject-evidence loading and the locked transaction-final recheck — lives in
the store package.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import (
    EVALUATOR_CONTRACT,
    MAXIMUM_RULES_PER_REVISION,
    EnforcedPolicyDecision,
    ExclusionPolicyRevision,
    ExclusionRule,
    PolicySubject,
    PolicySubjectField,
    RawPolicyDecision,
    RuleKind,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.evaluation import evaluate_policy
from personal_os.exclusion_policy.metrics import (
    EvaluationMetricOutcome,
    ExclusionPolicyMetrics,
    PolicyBoundary,
)
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.previews import compute_subject_fingerprint
from personal_os.exclusion_policy.signatures import (
    ED25519_PUBLIC_KEY_BYTES,
    ED25519_SIGNATURE_BYTES,
    SNAPSHOT_PAYLOAD_CONTRACT,
    SNAPSHOT_SIGNING_DOMAIN,
    PolicySignatureVerifier,
    build_signed_message,
    compute_payload_sha256_hex,
    derive_ed25519_key_id,
)
from personal_os.object_storage import CanonicalMediaType, ExpectedObject
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import (
    CreateSourceVersion,
    SourceType,
    UpdateSourceVersion,
)

if TYPE_CHECKING:
    from personal_os.sources.reading import CanonicalSourceReference

#: Closed reason token of the typed indeterminate denial (spec 19): the closed
#: ``reason`` detail names missing canonical evidence, never which operand.
REASON_REQUIRED_EVIDENCE_MISSING: SafeToken = SafeToken.parse("required_evidence_missing")

#: The pinned default decision of every published revision payload.
_DEFAULT_DECISION_ALLOWED: Final[str] = "allowed"

#: Raw evaluator decisions map one-to-one onto the closed metric label values.
_METRIC_OUTCOME_BY_RAW: Final[dict[RawPolicyDecision, EvaluationMetricOutcome]] = {
    RawPolicyDecision.ALLOWED: EvaluationMetricOutcome.ALLOWED,
    RawPolicyDecision.EXCLUDED: EvaluationMetricOutcome.EXCLUDED,
    RawPolicyDecision.INDETERMINATE: EvaluationMetricOutcome.INDETERMINATE,
}

#: The operand member each rule kind renders into the signed payload (spec 12).
_OPERAND_MEMBER_BY_KIND: Final[dict[RuleKind, str]] = {
    RuleKind.EXACT_SOURCE_ID: "source_id",
    RuleKind.FOLDER_PREFIX: "folder_prefix",
    RuleKind.PATH_GLOB: "path_glob",
    RuleKind.EXTENSION: "extension",
    RuleKind.MEDIA_TYPE: "media_type",
    RuleKind.MAXIMUM_SIZE: "maximum_size_bytes",
    RuleKind.SOURCE_TYPE: "source_type",
}

_HEX_LOWER: Final[frozenset[str]] = frozenset("0123456789abcdef")

#: Injectable clock returning the current aware UTC moment.
type AwareUtcClock = Callable[[], datetime]


def default_utc_clock() -> datetime:
    """The default aware UTC clock seam of the enforcement service."""

    return datetime.now(UTC)


def _validate_digest_hex(value: str) -> None:
    if len(value) != 64 or any(char not in _HEX_LOWER for char in value):
        raise ValueError("value must be 64 lowercase hexadecimal characters")


def policy_not_initialized_error() -> ExclusionPolicyError:
    """Build the typed missing-active-signed-policy denial (spec 14/19)."""

    return ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)


def signing_corruption_error(cause: BaseException | None = None) -> ExclusionPolicyError:
    """Build the typed corrupt-signature-material denial (spec 14/19)."""

    error = ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE)
    if cause is not None:
        raise error from cause
    return error


def policy_denied_error(revision_number: int) -> ExclusionPolicyError:
    """Build the typed definite-match denial carrying only the revision number."""

    return ExclusionPolicyError(
        ErrorCode.EXCLUSION_POLICY_DENIED,
        safe_details={"policy_revision_number": revision_number},
    )


def policy_indeterminate_error() -> ExclusionPolicyError:
    """Build the typed indeterminate denial carrying the closed reason token."""

    return ExclusionPolicyError(
        ErrorCode.EXCLUSION_POLICY_INDETERMINATE,
        safe_details={"reason": REASON_REQUIRED_EVIDENCE_MISSING},
    )


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Internal-only immutable evaluation evidence of one guarded operation.

    Carries the workspace and revision identities the decision was made under,
    the SHA-256 subject fingerprint (raw bytes of the hex digest; treated as
    sensitive despite being a hash), the exact raw and enforced decisions, the
    sorted matching rule IDs, the sorted missing subject fields and the
    evaluation instant. The value is never part of OpenAPI, MCP, Temporal
    history or plugin contracts; a guarded canonical operation re-checks
    workspace, active revision and subject fingerprint at its own
    transaction/read boundary rather than trusting this evidence.
    """

    workspace_id: UUID
    policy_revision_id: UUID
    revision_number: int
    subject_fingerprint: bytes
    raw_decision: RawPolicyDecision
    enforced_decision: EnforcedPolicyDecision
    matched_rule_ids: tuple[UUID, ...]
    missing_fields: tuple[PolicySubjectField, ...]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("policy_revision_id", self.policy_revision_id)
        if self.revision_number < 1:
            raise ValueError("revision_number must be at least 1")
        if len(self.subject_fingerprint) != 32:
            raise ValueError("subject_fingerprint must be exactly 32 bytes")
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AllowedPolicyRevisionBinding:
    """Immutable server-owned evidence that one policy revision allowed a preflight."""

    workspace_id: UUID
    policy_revision_number: int

    def __post_init__(self) -> None:
        reject_nil_uuid("workspace_id", self.workspace_id)
        if self.policy_revision_number < 1:
            raise ValueError("policy_revision_number must be positive")


type PublicationPolicyEvidence = PolicyDecision | AllowedPolicyRevisionBinding


@dataclass(frozen=True, slots=True)
class ActivePolicySnapshotMaterial:
    """The persisted signed members of one active revision plus its trust anchor.

    Assembled by the store half from ``workspace_policy_state``,
    ``source_policies`` and the joined ``policy_signing_keys`` row. The public
    key bytes are the revision's own persisted trust anchor, so verification
    proves the snapshot was signed by exactly the key the workspace committed.
    """

    workspace_id: UUID
    policy_revision_id: UUID
    revision_number: int
    payload_bytes: bytes
    payload_sha256: str
    signature_bytes: bytes
    public_key_bytes: bytes

    def __post_init__(self) -> None:
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("policy_revision_id", self.policy_revision_id)
        if self.revision_number < 1:
            raise ValueError("revision_number must be at least 1")
        if not self.payload_bytes:
            raise ValueError("payload_bytes must not be empty")
        _validate_digest_hex(self.payload_sha256)
        if len(self.signature_bytes) != ED25519_SIGNATURE_BYTES:
            raise ValueError("signature_bytes must be exactly 64 raw Ed25519 bytes")
        if len(self.public_key_bytes) != ED25519_PUBLIC_KEY_BYTES:
            raise ValueError("public_key_bytes must be exactly 32 raw Ed25519 bytes")


class ActivePolicySnapshotSource(Protocol):
    """Port loading the active signed snapshot material of one workspace.

    Returns ``None`` when the workspace has no published active revision; the
    caller maps that to the typed not-initialized denial. The preflight load
    is a non-authoritative hint: the authoritative resolution always happens
    under the policy-state row lock inside the guarded transaction.
    """

    async def load_active_snapshot(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> ActivePolicySnapshotMaterial | None: ...


class PolicySubjectEvidenceSource(Protocol):
    """Port loading the canonical stored subject evidence of one source.

    Returns the workspace-bound subject with the source's stored type evidence
    (canonical source state carries no locator yet), or ``None`` when the
    source does not exist under the workspace.
    """

    async def load_subject_evidence(
        self, workspace_id: UUID, source_id: UUID, context: DiagnosticContext
    ) -> PolicySubject | None: ...


class PolicyTrustAnchorVerifier(Protocol):
    """Port verifying one snapshot signature under exactly the given anchor."""

    def verify(
        self, *, public_key_bytes: bytes, signature_bytes: bytes, message: bytes
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class KeyedTrustAnchorVerifier:
    """Adapt the closed keyed signature port to trust-anchor-byte verification.

    The key ID handed to the wrapped verifier is always the one derived from
    the provided trust-anchor bytes, so a mapping mismatch fails verification
    instead of silently trusting a different key.
    """

    keyed_verifier: PolicySignatureVerifier

    def verify(self, *, public_key_bytes: bytes, signature_bytes: bytes, message: bytes) -> bool:
        return self.keyed_verifier.verify(
            derive_ed25519_key_id(public_key_bytes), signature_bytes, message
        )


def parse_verified_policy_revision(
    material: ActivePolicySnapshotMaterial,
    *,
    verifier: PolicyTrustAnchorVerifier,
) -> ExclusionPolicyRevision:
    """Verify one persisted snapshot and rebuild its immutable revision.

    Fails closed on every violation: a payload hash mismatch, a failed or
    malformed signature, a non-canonical JSON document, a wrong contract tag,
    a workspace/revision/number drift between the row and the payload, a
    wrong default decision or evaluator-contract hash, an unknown rule kind
    or an operand outside the closed grammar. Every failure is the typed
    signing-unavailable denial with the cause chained only — no payload byte,
    operand, key material or library text enters the error.
    """

    try:
        if compute_payload_sha256_hex(material.payload_bytes) != material.payload_sha256:
            raise ValueError("payload hash mismatch")
        message = build_signed_message(SNAPSHOT_SIGNING_DOMAIN, material.payload_bytes)
        if not verifier.verify(
            public_key_bytes=material.public_key_bytes,
            signature_bytes=material.signature_bytes,
            message=message,
        ):
            raise ValueError("signature verification failed")
        return _parse_snapshot_payload(material)
    except ExclusionPolicyError:
        raise
    except Exception as cause:
        raise signing_corruption_error(cause) from cause


def _parse_snapshot_payload(
    material: ActivePolicySnapshotMaterial,
) -> ExclusionPolicyRevision:
    """Rebuild the immutable revision from the canonical payload document."""

    document = json.loads(material.payload_bytes)
    if not isinstance(document, dict):
        raise ValueError("snapshot payload must be a JSON object")
    if document.get("contract") != SNAPSHOT_PAYLOAD_CONTRACT:
        raise ValueError("snapshot payload contract mismatch")
    if document.get("workspace_id") != str(material.workspace_id):
        raise ValueError("snapshot payload workspace mismatch")
    if document.get("policy_revision_id") != str(material.policy_revision_id):
        raise ValueError("snapshot payload revision identity mismatch")
    revision_number = document.get("revision_number")
    if isinstance(revision_number, bool) or not isinstance(revision_number, int):
        raise ValueError("snapshot payload revision number must be an integer")
    if revision_number != material.revision_number:
        raise ValueError("snapshot payload revision number mismatch")
    if document.get("default_decision") != _DEFAULT_DECISION_ALLOWED:
        raise ValueError("snapshot payload default decision mismatch")
    if (
        document.get("evaluator_contract_sha256")
        != sha256(EVALUATOR_CONTRACT.encode("ascii")).hexdigest()
    ):
        raise ValueError("snapshot payload evaluator contract mismatch")
    rules_payload = document.get("rules")
    if not isinstance(rules_payload, list):
        raise ValueError("snapshot payload rules must be an array")
    if len(rules_payload) > MAXIMUM_RULES_PER_REVISION:
        raise ValueError("snapshot payload exceeds the rule ceiling")
    rules = tuple(
        _parse_snapshot_rule(rule_payload, rule_index)
        for rule_index, rule_payload in enumerate(rules_payload)
    )
    return ExclusionPolicyRevision(
        policy_revision_id=material.policy_revision_id,
        workspace_id=material.workspace_id,
        revision_number=material.revision_number,
        rules=rules,
    )


def _parse_snapshot_rule(rule_payload: object, rule_index: int) -> ExclusionRule:
    """Rebuild one signed rule through the sanctioned normalization path."""

    if not isinstance(rule_payload, dict):
        raise ValueError("signed rule must be a JSON object")
    rule_id = rule_payload.get("rule_id")
    if not isinstance(rule_id, str):
        raise ValueError("signed rule requires a rule_id string")
    rule_kind_value = rule_payload.get("rule_kind")
    if not isinstance(rule_kind_value, str):
        raise ValueError("signed rule requires a rule_kind string")
    rule_kind = RuleKind(rule_kind_value)
    operand_member = _OPERAND_MEMBER_BY_KIND[rule_kind]
    if rule_kind is RuleKind.EXACT_SOURCE_ID:
        source_id_value = rule_payload.get(operand_member)
        if not isinstance(source_id_value, str):
            raise ValueError("exact_source_id operand must be a string")
        return normalize_rule(
            UUID(rule_id),
            rule_kind,
            source_id_operand=UUID(source_id_value),
            rule_index=rule_index,
        )
    if rule_kind is RuleKind.MAXIMUM_SIZE:
        size_operand = rule_payload.get(operand_member)
        if isinstance(size_operand, bool) or not isinstance(size_operand, int):
            raise ValueError("maximum_size operand must be an integer")
        return normalize_rule(
            UUID(rule_id),
            rule_kind,
            size_bytes_operand=size_operand,
            rule_index=rule_index,
        )
    text_operand = rule_payload.get(operand_member)
    if not isinstance(text_operand, str):
        raise ValueError("signed rule operand must be a string")
    return normalize_rule(
        UUID(rule_id),
        rule_kind,
        text_operand=text_operand,
        rule_index=rule_index,
    )


def evaluate_policy_decision(
    *,
    revision: ExclusionPolicyRevision,
    subject: PolicySubject,
    evaluated_at: datetime,
) -> PolicyDecision:
    """Evaluate one subject against one verified immutable revision."""

    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    outcome = evaluate_policy(revision=revision, subject=subject)
    return PolicyDecision(
        workspace_id=revision.workspace_id,
        policy_revision_id=revision.policy_revision_id,
        revision_number=revision.revision_number,
        subject_fingerprint=bytes.fromhex(compute_subject_fingerprint(subject)),
        raw_decision=outcome.raw,
        enforced_decision=outcome.enforced,
        matched_rule_ids=outcome.matched_rule_ids,
        missing_fields=outcome.missing_fields,
        evaluated_at=evaluated_at,
    )


def enforce_policy_decision(decision: PolicyDecision) -> None:
    """Raise the typed denial unless the enforced decision allows the subject."""

    if decision.raw_decision is RawPolicyDecision.EXCLUDED:
        raise policy_denied_error(decision.revision_number)
    if decision.raw_decision is RawPolicyDecision.INDETERMINATE:
        raise policy_indeterminate_error()


def record_evaluation_metric(
    metrics: ExclusionPolicyMetrics,
    *,
    boundary: PolicyBoundary,
    decision: PolicyDecision,
    duration_seconds: float,
) -> None:
    """Record one evaluation outcome with only the closed metric labels."""

    metrics.record_evaluation(
        boundary=boundary,
        decision=_METRIC_OUTCOME_BY_RAW[decision.raw_decision],
        duration_seconds=max(duration_seconds, 0.0),
    )


class PolicyEnforcementService:
    """Provider-neutral enforcement application service (spec 14).

    ``authorize_preflight`` loads the active signed snapshot, verifies it
    against its persisted trust anchor, evaluates the candidate subject and
    either returns the internal :class:`PolicyDecision` or raises the typed
    denial. ``authorize_publication`` and ``authorize_read`` build the
    boundary-specific candidate subject and delegate; the composition root
    binds the snapshot/evidence ports to the store adapters and the verifier
    to the Ed25519 trust-anchor adapter. Decisions returned here are
    non-authoritative hints: the guarded PostgreSQL transaction re-evaluates
    under the policy-state row lock before any canonical mutation.
    """

    def __init__(
        self,
        *,
        snapshot_source: ActivePolicySnapshotSource,
        evidence_source: PolicySubjectEvidenceSource,
        verifier: PolicyTrustAnchorVerifier,
        metrics: ExclusionPolicyMetrics | None = None,
        clock: AwareUtcClock | None = None,
    ) -> None:
        self._snapshot_source = snapshot_source
        self._evidence_source = evidence_source
        self._verifier = verifier
        self._metrics = metrics
        self._clock = clock if clock is not None else default_utc_clock

    async def authorize_preflight(
        self,
        *,
        subject: PolicySubject,
        boundary: PolicyBoundary,
        context: DiagnosticContext,
    ) -> PolicyDecision:
        """Verify the active signed policy and evaluate one candidate subject."""

        started = time.monotonic()
        material = await self._snapshot_source.load_active_snapshot(subject.workspace_id, context)
        if material is None:
            raise policy_not_initialized_error()
        decision = self._evaluate_material(material, subject, boundary, started)
        enforce_policy_decision(decision)
        return decision

    async def authorize_publication(
        self,
        command: CreateSourceVersion | UpdateSourceVersion,
        context: DiagnosticContext,
    ) -> PolicyDecision:
        """Evaluate one publication candidate before any object-store access.

        A create carries its full evidence: the declared source type plus the
        expected object's media type and size. An update carries the expected
        object evidence and resolves the stored source type through the
        evidence port — the canonical source type is immutable, so a missing
        row simply yields a subject without type evidence, which fails closed
        through the normal indeterminate path whenever a rule needs it.
        """

        expected = command.expected_object
        subject: PolicySubject
        if isinstance(command, CreateSourceVersion):
            subject = PolicySubject(
                workspace_id=command.workspace_id,
                source_id=command.source_id,
                source_type=command.source_type,
                media_type=expected.media_type,
                size_bytes=expected.size_bytes,
            )
        else:
            stored = await self._evidence_source.load_subject_evidence(
                command.workspace_id, command.source_id, context
            )
            source_type = stored.source_type if stored is not None else None
            subject = PolicySubject(
                workspace_id=command.workspace_id,
                source_id=command.source_id,
                source_type=source_type,
                media_type=expected.media_type,
                size_bytes=expected.size_bytes,
            )
        return await self.authorize_preflight(
            subject=subject, boundary=PolicyBoundary.SINGLE_PART_UPLOAD, context=context
        )

    async def authorize_read(
        self, reference: CanonicalSourceReference, context: DiagnosticContext
    ) -> PolicyDecision:
        """Evaluate one resolved canonical reference before any object GET."""

        expected_object: ExpectedObject = reference.expected_object
        media_type: CanonicalMediaType = expected_object.media_type
        source_type: SourceType = reference.source_type
        subject = PolicySubject(
            workspace_id=reference.workspace_id,
            source_id=reference.source_id,
            source_type=source_type,
            media_type=media_type,
            size_bytes=expected_object.size_bytes,
        )
        return await self.authorize_preflight(
            subject=subject, boundary=PolicyBoundary.CANONICAL_READ, context=context
        )

    def evaluate_material(
        self,
        material: ActivePolicySnapshotMaterial,
        *,
        subject: PolicySubject,
        boundary: PolicyBoundary,
    ) -> PolicyDecision:
        """Verify, parse and evaluate one loaded snapshot material value."""

        return self._evaluate_material(material, subject, boundary, time.monotonic())

    def _evaluate_material(
        self,
        material: ActivePolicySnapshotMaterial,
        subject: PolicySubject,
        boundary: PolicyBoundary,
        started_monotonic: float,
    ) -> PolicyDecision:
        revision = parse_verified_policy_revision(material, verifier=self._verifier)
        decision = evaluate_policy_decision(
            revision=revision, subject=subject, evaluated_at=self._clock()
        )
        self._record(boundary, decision, started_monotonic)
        return decision

    def _record(
        self,
        boundary: PolicyBoundary,
        decision: PolicyDecision,
        started_monotonic: float,
    ) -> None:
        if self._metrics is None:
            return
        record_evaluation_metric(
            self._metrics,
            boundary=boundary,
            decision=decision,
            duration_seconds=time.monotonic() - started_monotonic,
        )


__all__ = [
    "REASON_REQUIRED_EVIDENCE_MISSING",
    "ActivePolicySnapshotMaterial",
    "ActivePolicySnapshotSource",
    "AllowedPolicyRevisionBinding",
    "AwareUtcClock",
    "KeyedTrustAnchorVerifier",
    "PolicyDecision",
    "PolicyEnforcementService",
    "PolicySubjectEvidenceSource",
    "PolicyTrustAnchorVerifier",
    "PublicationPolicyEvidence",
    "default_utc_clock",
    "enforce_policy_decision",
    "evaluate_policy_decision",
    "parse_verified_policy_revision",
    "policy_denied_error",
    "policy_indeterminate_error",
    "policy_not_initialized_error",
    "record_evaluation_metric",
    "signing_corruption_error",
]
