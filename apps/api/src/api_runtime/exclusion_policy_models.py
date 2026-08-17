"""Strict exclusion-policy wire models and the domain boundary conversion.

Every model here is frozen and closed for extra fields, mirrors the member
grammar of the signed snapshot/keyset payloads of spec 12/13 (one
``rule_id``, one ``rule_kind`` and exactly one named operand member) and
never carries a workspace, device, actor, signature or revision-number
selector — workspace and actor always arrive from the authenticated context.
Conversion to domain values happens only through the shared normalization
gate, so a rejected operand surfaces as the typed
``exclusion_policy_input_invalid`` with its closed reason token and optional
zero-based ``rule_index`` and never echoes the rejected value. The response
renderers project domain values onto strict payloads: draft rules keep their
semantic fingerprint, preview rows carry only opaque IDs and closed states —
never a subject fingerprint — and the signed envelope renderers re-validate
the persisted canonical payload bytes against the closed schemas instead of
trusting them, mapping any divergence to the safe ``internal_error``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.exclusion_policy.contracts import (
    ExactSourceIdOperand,
    ExclusionRule,
    ExtensionOperand,
    FolderPrefixOperand,
    MaximumSizeOperand,
    MediaTypeOperand,
    PathGlobOperand,
    RuleKind,
    SourceTypeOperand,
)
from personal_os.exclusion_policy.errors import (
    OPERAND_CONFLICT,
    OPERAND_MISSING,
    ExclusionPolicyError,
)
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.ports import PolicyDraft, PolicyKeysetRecord
from personal_os.exclusion_policy.previews import (
    PolicyPreviewRecord,
    PolicyPreviewResultPage,
)
from personal_os.exclusion_policy.publication import PublishedPolicyResult
from personal_os.exclusion_policy.signatures import (
    derive_ed25519_key_id,
    encode_base64url_without_padding,
)

#: The single named operand member of each closed rule kind; request bodies,
#: draft responses and signed payloads share this exact grammar.
RULE_OPERAND_MEMBERS: Final[dict[RuleKind, str]] = {
    RuleKind.EXACT_SOURCE_ID: "source_id",
    RuleKind.FOLDER_PREFIX: "folder_prefix",
    RuleKind.PATH_GLOB: "path_glob",
    RuleKind.EXTENSION: "extension",
    RuleKind.MEDIA_TYPE: "media_type",
    RuleKind.MAXIMUM_SIZE: "maximum_size_bytes",
    RuleKind.SOURCE_TYPE: "source_type",
}

#: Every operand member name, for the exactly-one-operand wire checks.
_ALL_OPERAND_MEMBERS: Final[frozenset[str]] = frozenset(RULE_OPERAND_MEMBERS.values())

#: Wire grammar of the lowercase digest members (64 hexadecimal characters).
_DIGEST_PATTERN: Final[str] = r"^[0-9a-f]{64}$"

#: Wire grammar of the derived Ed25519 key identifier.
_POLICY_KEY_ID_PATTERN: Final[str] = r"^ed25519-sha256-[A-Za-z0-9_-]{43}$"

#: Wire grammar of base64url values without padding.
_BASE64URL_PATTERN: Final[str] = r"^[A-Za-z0-9_-]+$"

#: The closed UTC timestamp spelling of the signed payloads.
_UTC_TIMESTAMP_PATTERN: Final[str] = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[.][0-9]{6}Z$"
)

PreviewStatusValue = Literal[
    "pending", "leased", "running", "ready", "failed", "expired", "consumed"
]
PreviewImpactClassValue = Literal[
    "newly_excluded", "still_excluded", "newly_allowed", "still_allowed", "indeterminate"
]
KeysetStateValue = Literal["current", "staged", "retired"]


class PolicyDraftRuleRequest(BaseModel):
    """One desired draft rule in the shared signed-payload member grammar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: UUID
    rule_kind: RuleKind
    source_id: UUID | None = None
    folder_prefix: str | None = None
    path_glob: str | None = None
    extension: str | None = None
    media_type: str | None = None
    maximum_size_bytes: int | None = None
    source_type: str | None = None


class PolicyDraftReplaceRequest(BaseModel):
    """The strict full-list draft replacement body (spec 16.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_draft_version: int = Field(ge=1)
    rules: tuple[PolicyDraftRuleRequest, ...] = Field(max_length=256)


class PolicyPublicationRequest(BaseModel):
    """The exact expected publication binding (spec 11/16.1).

    Carries the ready preview identity, the expected draft identity, version
    and semantic digest, the preview impact digest, the expected active
    revision and the exact confirmation phrase — never a client-supplied
    signature, revision allocation or workspace selector. The opaque
    idempotency key travels in its dedicated header.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_preview_id: UUID
    policy_draft_id: UUID
    expected_draft_version: int = Field(ge=1)
    expected_draft_sha256: str = Field(pattern=_DIGEST_PATTERN)
    preview_impact_digest: str = Field(pattern=_DIGEST_PATTERN)
    expected_active_policy_revision_id: UUID | None = None
    expected_active_revision_number: int = Field(ge=0)
    confirmation: str


class PolicyRuleData(BaseModel):
    """One rendered draft rule: identity, kind, fingerprint and one operand.

    Exactly one operand member is populated at construction and the policy
    responses render with ``exclude_unset``, so the emitted rule carries the
    same one-named-operand grammar as the signed payloads of spec 12.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: UUID
    rule_kind: RuleKind
    semantic_fingerprint: str = Field(pattern=_DIGEST_PATTERN)
    source_id: UUID | None = None
    folder_prefix: str | None = None
    path_glob: str | None = None
    extension: str | None = None
    media_type: str | None = None
    maximum_size_bytes: int | None = None
    source_type: str | None = None


class PolicyDraftData(BaseModel):
    """The working draft with its exact version (spec 16.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    draft_id: UUID
    draft_version: int
    base_policy_revision_id: UUID | None
    rules: tuple[PolicyRuleData, ...]


class PolicyReconciliationSummaryData(BaseModel):
    """The closed reconciliation summary of the active revision (spec 15)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_revision_id: UUID
    state: str
    updated_at: datetime


class ExclusionPolicyStatusData(BaseModel):
    """The Admin status read: revision metadata, draft and reconciliation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    active_policy_revision_id: UUID | None
    active_revision_number: int
    draft: PolicyDraftData
    reconciliation: PolicyReconciliationSummaryData | None


class PolicyPreviewCountersData(BaseModel):
    """The five closed impact counters of one preview (spec 10)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    newly_excluded_count: int
    still_excluded_count: int
    newly_allowed_count: int
    still_allowed_count: int
    indeterminate_count: int


class PolicyPreviewResultRowData(BaseModel):
    """One preview result row: opaque IDs and closed states only (spec 10)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: UUID
    previous_raw_decision: str
    previous_enforced_decision: str
    proposed_raw_decision: str
    proposed_enforced_decision: str
    proposed_match_state: str
    impact_class: PreviewImpactClassValue
    matched_rule_ids: tuple[UUID, ...]
    missing_fields: tuple[str, ...]


class PolicyPreviewCursorData(BaseModel):
    """The stable ``(impact_class, source_id)`` continuation cursor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    impact_class: PreviewImpactClassValue
    source_id: UUID


class PolicyPreviewData(BaseModel):
    """One preview lifecycle read; ``results`` render only once ready."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_preview_id: UUID
    status: PreviewStatusValue
    policy_draft_id: UUID
    draft_version: int
    draft_sha256: str
    base_policy_revision_id: UUID | None
    source_checkpoint_event_sequence: int
    created_at: datetime
    ready_at: datetime | None
    expires_at: datetime | None
    consumed_at: datetime | None
    impact_digest: str | None
    safe_error_code: str | None
    counters: PolicyPreviewCountersData
    results: tuple[PolicyPreviewResultRowData, ...] | None = None
    next_cursor: PolicyPreviewCursorData | None = None


class PolicyPublicationData(BaseModel):
    """The durable publication outcome; never payload or signature bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: UUID
    policy_revision_id: UUID
    revision_number: int
    parent_policy_revision_id: UUID | None
    payload_sha256: str = Field(pattern=_DIGEST_PATTERN)
    signing_key_id: str = Field(pattern=_POLICY_KEY_ID_PATTERN)
    published_at: datetime
    rule_count: int
    reconciliation_status: str
    is_replay: bool


class PolicySnapshotRuleData(BaseModel):
    """One rule of a signed snapshot payload (spec 12): no fingerprint.

    Exactly one operand member is populated at construction and the policy
    responses render with ``exclude_unset``, so the emitted payload mirrors
    the persisted canonical member set.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: UUID
    rule_kind: RuleKind
    source_id: UUID | None = None
    folder_prefix: str | None = None
    path_glob: str | None = None
    extension: str | None = None
    media_type: str | None = None
    maximum_size_bytes: int | None = None
    source_type: str | None = None


class PolicySnapshotPayloadData(BaseModel):
    """The re-validated canonical snapshot payload of spec 12."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: Literal["exclusion_policy_snapshot/v1"]
    workspace_id: UUID
    policy_revision_id: UUID
    revision_number: int = Field(ge=1)
    parent_policy_revision_id: UUID | None
    published_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)
    default_decision: Literal["allowed"]
    evaluator_contract_sha256: str = Field(pattern=_DIGEST_PATTERN)
    rules: tuple[PolicySnapshotRuleData, ...]


class PolicySnapshotSignatureData(BaseModel):
    """The detached Ed25519 signature member of the spec 12 envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm: Literal["Ed25519"]
    key_id: str = Field(pattern=_POLICY_KEY_ID_PATTERN)
    value: str = Field(pattern=_BASE64URL_PATTERN, min_length=86, max_length=86)


class SignedPolicySnapshotData(BaseModel):
    """The exact persisted signed-snapshot envelope as typed JSON."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload: PolicySnapshotPayloadData
    payload_sha256: str = Field(pattern=_DIGEST_PATTERN)
    signature: PolicySnapshotSignatureData


class PolicyKeysetKeyData(BaseModel):
    """One trust-anchor entry of a canonical keyset payload (spec 13)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm: Literal["Ed25519"]
    key_id: str = Field(pattern=_POLICY_KEY_ID_PATTERN)
    public_key: str = Field(pattern=_BASE64URL_PATTERN, min_length=43, max_length=43)
    state: KeysetStateValue


class PolicyKeysetPayloadData(BaseModel):
    """The re-validated canonical keyset payload of spec 13."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: Literal["exclusion_policy_keyset/v1"]
    workspace_id: UUID
    keyset_revision: int = Field(ge=1)
    parent_keyset_revision: int | None
    created_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)
    keys: tuple[PolicyKeysetKeyData, ...]


class PolicyKeysetSignatureData(BaseModel):
    """One cross-signature over the canonical keyset payload (spec 13)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm: Literal["Ed25519"]
    key_id: str = Field(pattern=_POLICY_KEY_ID_PATTERN)
    value: str = Field(pattern=_BASE64URL_PATTERN, min_length=86, max_length=86)


class PolicyKeysetEnvelopeData(BaseModel):
    """One persisted keyset envelope: payload, digest and cross-signatures."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload: PolicyKeysetPayloadData
    payload_sha256: str = Field(pattern=_DIGEST_PATTERN)
    signatures: tuple[PolicyKeysetSignatureData, ...]


class PolicyKeysetPageData(BaseModel):
    """One bounded, ordered keyset chain page with its continuation flag."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    keysets: tuple[PolicyKeysetEnvelopeData, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class ActivePolicySnapshot:
    """The persisted signed members of the active revision (read value)."""

    policy_revision_id: UUID
    revision_number: int
    parent_policy_revision_id: UUID | None
    payload_bytes: bytes
    payload_sha256: str
    signing_key_id: str
    signature_bytes: bytes
    published_at: datetime


@dataclass(frozen=True, slots=True)
class PolicyReconciliationSummary:
    """The latest durable reconciliation intent of one workspace."""

    policy_revision_id: UUID
    state: str
    updated_at: datetime


def to_domain_rule(rule: PolicyDraftRuleRequest, rule_index: int) -> ExclusionRule:
    """Convert one wire rule into its immutable domain value (typed errors).

    The kind's own operand member must be present and every other operand
    member absent — a missing member is the closed ``operand_missing``, a
    populated foreign member the closed ``operand_conflict`` — then the
    shared normalization gate validates the value itself, so every rejection
    carries exactly one closed reason token plus the zero-based index.
    """

    own_member = RULE_OPERAND_MEMBERS[rule.rule_kind]
    own_value = getattr(rule, own_member)
    foreign_members = [
        member
        for member in _ALL_OPERAND_MEMBERS
        if member != own_member and getattr(rule, member) is not None
    ]
    if foreign_members:
        raise ExclusionPolicyError(
            ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
            safe_details={"reason": OPERAND_CONFLICT, "rule_index": rule_index},
        )
    if own_value is None:
        raise ExclusionPolicyError(
            ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
            safe_details={"reason": OPERAND_MISSING, "rule_index": rule_index},
        )
    if rule.rule_kind is RuleKind.EXACT_SOURCE_ID:
        return normalize_rule(
            rule.rule_id, rule.rule_kind, source_id_operand=rule.source_id, rule_index=rule_index
        )
    if rule.rule_kind is RuleKind.MAXIMUM_SIZE:
        return normalize_rule(
            rule.rule_id,
            rule.rule_kind,
            size_bytes_operand=rule.maximum_size_bytes,
            rule_index=rule_index,
        )
    return normalize_rule(
        rule.rule_id,
        rule.rule_kind,
        text_operand=str(own_value),
        rule_index=rule_index,
    )


def _operand_member(rule: ExclusionRule) -> tuple[str, object]:
    """Render ``(member, value)`` of one rule's single typed operand."""

    operand = rule.operand
    if isinstance(operand, ExactSourceIdOperand):
        return "source_id", str(operand.source_id)
    if isinstance(operand, FolderPrefixOperand):
        return "folder_prefix", operand.folder_prefix
    if isinstance(operand, PathGlobOperand):
        return "path_glob", operand.normalized_pattern
    if isinstance(operand, ExtensionOperand):
        return "extension", operand.extension
    if isinstance(operand, MediaTypeOperand):
        return "media_type", (
            operand.exact_media_type.value
            if operand.exact_media_type is not None
            else f"{operand.family_type}/*"
        )
    if isinstance(operand, MaximumSizeOperand):
        return "maximum_size_bytes", operand.maximum_size_bytes
    assert isinstance(operand, SourceTypeOperand)
    return "source_type", operand.source_type.value


def _rule_data(rule: ExclusionRule) -> PolicyRuleData:
    member, value = _operand_member(rule)
    fields: dict[str, object] = {
        "rule_id": rule.rule_id,
        "rule_kind": rule.rule_kind,
        "semantic_fingerprint": rule.semantic_fingerprint,
        member: value,
    }
    return PolicyRuleData.model_validate(fields)


def _snapshot_rule_data(rule: ExclusionRule) -> PolicySnapshotRuleData:
    member, value = _operand_member(rule)
    fields: dict[str, object] = {
        "rule_id": rule.rule_id,
        "rule_kind": rule.rule_kind,
        member: value,
    }
    return PolicySnapshotRuleData.model_validate(fields)


def policy_draft_data(draft: PolicyDraft) -> PolicyDraftData:
    """Render one immutable draft snapshot as the strict wire payload."""

    return PolicyDraftData(
        draft_id=draft.draft_id,
        draft_version=draft.draft_version,
        base_policy_revision_id=draft.base_policy_revision_id,
        rules=tuple(_rule_data(rule) for rule in draft.rules),
    )


def policy_preview_data(
    record: PolicyPreviewRecord, page: PolicyPreviewResultPage | None = None
) -> PolicyPreviewData:
    """Render one preview lifecycle record, optionally with one result page."""

    results: tuple[PolicyPreviewResultRowData, ...] | None = None
    next_cursor: PolicyPreviewCursorData | None = None
    if page is not None:
        results = tuple(
            PolicyPreviewResultRowData(
                source_id=row.source_id,
                previous_raw_decision=row.previous_raw_decision.value,
                previous_enforced_decision=row.previous_enforced_decision.value,
                proposed_raw_decision=row.proposed_raw_decision.value,
                proposed_enforced_decision=row.proposed_enforced_decision.value,
                proposed_match_state=row.proposed_match_state.value,
                impact_class=row.impact_class.value,
                matched_rule_ids=row.matched_rule_ids,
                missing_fields=tuple(field.value for field in row.missing_fields),
            )
            for row in page.rows
        )
        if page.next_cursor is not None:
            next_cursor = PolicyPreviewCursorData(
                impact_class=page.next_cursor.impact_class.value,
                source_id=page.next_cursor.source_id,
            )
    return PolicyPreviewData(
        policy_preview_id=record.policy_preview_id,
        status=record.status.value,
        policy_draft_id=record.policy_draft_id,
        draft_version=record.draft_version,
        draft_sha256=record.draft_sha256,
        base_policy_revision_id=record.base_policy_revision_id,
        source_checkpoint_event_sequence=record.source_checkpoint_event_sequence,
        created_at=record.created_at,
        ready_at=record.ready_at,
        expires_at=record.expires_at,
        consumed_at=record.consumed_at,
        impact_digest=record.impact_digest,
        safe_error_code=record.safe_error_code,
        counters=PolicyPreviewCountersData(
            newly_excluded_count=record.newly_excluded_count,
            still_excluded_count=record.still_excluded_count,
            newly_allowed_count=record.newly_allowed_count,
            still_allowed_count=record.still_allowed_count,
            indeterminate_count=record.indeterminate_count,
        ),
        results=results,
        next_cursor=next_cursor,
    )


def policy_publication_data(result: PublishedPolicyResult) -> PolicyPublicationData:
    """Render one durable publication outcome as the strict wire payload."""

    return PolicyPublicationData(
        workspace_id=result.workspace_id,
        policy_revision_id=result.policy_revision_id,
        revision_number=result.revision_number,
        parent_policy_revision_id=result.parent_policy_revision_id,
        payload_sha256=result.payload_sha256,
        signing_key_id=result.signing_key_id,
        published_at=result.published_at,
        rule_count=result.rule_count,
        reconciliation_status=result.reconciliation_status,
        is_replay=result.is_replay,
    )


def _parse_canonical_payload(payload_bytes: bytes) -> dict[str, object]:
    """Parse persisted canonical bytes, failing closed without echoing them."""

    try:
        parsed = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as cause:
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause
    if not isinstance(parsed, dict):
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from None
    return parsed


def signed_snapshot_data(snapshot: ActivePolicySnapshot) -> SignedPolicySnapshotData:
    """Render the exact persisted signed-snapshot envelope as typed JSON.

    The payload bytes are the persisted canonical JCS rendering; they are
    re-validated against the closed payload schema — a divergence is
    corruption and fails closed as the safe ``internal_error`` — and the
    signature member carries the derived key ID and base64url value.
    """

    payload = PolicySnapshotPayloadData.model_validate(
        _parse_canonical_payload(snapshot.payload_bytes)
    )
    return SignedPolicySnapshotData(
        payload=payload,
        payload_sha256=snapshot.payload_sha256,
        signature=PolicySnapshotSignatureData(
            algorithm="Ed25519",
            key_id=snapshot.signing_key_id,
            value=encode_base64url_without_padding(snapshot.signature_bytes),
        ),
    )


def policy_keyset_envelope_data(record: PolicyKeysetRecord) -> PolicyKeysetEnvelopeData:
    """Render one persisted keyset record as the strict envelope payload.

    Each cross-signature references its signing-key row; the derived key ID
    of that row's public bytes is the identifier the spec 13 envelope
    carries, and signatures render sorted by that identifier so the envelope
    is deterministic for one persisted row set.
    """

    payload = PolicyKeysetPayloadData.model_validate(
        _parse_canonical_payload(record.canonical_payload_bytes)
    )
    key_ids_by_row: dict[UUID, str] = {
        key.signing_key_id: derive_ed25519_key_id(key.public_key_bytes) for key in record.keys
    }
    rendered_signatures = sorted(
        (
            PolicyKeysetSignatureData(
                algorithm="Ed25519",
                key_id=key_ids_by_row[signature.signing_key_id],
                value=encode_base64url_without_padding(signature.signature_bytes),
            )
            for signature in record.signatures
        ),
        key=lambda signature: signature.key_id,
    )
    return PolicyKeysetEnvelopeData(
        payload=payload,
        payload_sha256=record.payload_sha256,
        signatures=tuple(rendered_signatures),
    )


def policy_keyset_page_data(
    records: tuple[PolicyKeysetRecord, ...], *, has_more: bool
) -> PolicyKeysetPageData:
    """Render one bounded ordered keyset chain page."""

    return PolicyKeysetPageData(
        keysets=tuple(policy_keyset_envelope_data(record) for record in records),
        has_more=has_more,
    )


__all__ = [
    "RULE_OPERAND_MEMBERS",
    "ActivePolicySnapshot",
    "ExclusionPolicyStatusData",
    "PolicyDraftData",
    "PolicyDraftReplaceRequest",
    "PolicyDraftRuleRequest",
    "PolicyKeysetEnvelopeData",
    "PolicyKeysetKeyData",
    "PolicyKeysetPageData",
    "PolicyKeysetPayloadData",
    "PolicyKeysetSignatureData",
    "PolicyPreviewCountersData",
    "PolicyPreviewCursorData",
    "PolicyPreviewData",
    "PolicyPreviewResultRowData",
    "PolicyPublicationData",
    "PolicyPublicationRequest",
    "PolicyReconciliationSummary",
    "PolicyReconciliationSummaryData",
    "PolicyRuleData",
    "PolicySnapshotPayloadData",
    "PolicySnapshotRuleData",
    "PolicySnapshotSignatureData",
    "SignedPolicySnapshotData",
    "policy_draft_data",
    "policy_keyset_envelope_data",
    "policy_keyset_page_data",
    "policy_preview_data",
    "policy_publication_data",
    "signed_snapshot_data",
    "to_domain_rule",
]
