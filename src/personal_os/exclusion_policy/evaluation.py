"""The pure deny-only evaluator over normalized policy revisions.

Evaluation is deterministic and I/O-free: rules are deny-only with default
allow, so one or more definite matches yield ``excluded``, otherwise any
required subject field missing from the canonical evidence yields
``indeterminate`` (which enforcement maps to ``excluded``), and otherwise the
decision is ``allowed``. A definite match always wins over unrelated missing
evidence, and an empty revision allows every otherwise valid subject.

Evidence carries only sorted matching rule IDs and sorted missing field
names — never a locator, operand, path, title or subject fingerprint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    ExactSourceIdOperand,
    ExclusionPolicyRevision,
    ExtensionOperand,
    FolderPrefixOperand,
    MaximumSizeOperand,
    MediaTypeOperand,
    PathGlobOperand,
    PolicySubject,
    PolicySubjectField,
    RawPolicyDecision,
    RuleKind,
    RuleOperand,
    SourceTypeOperand,
)
from personal_os.exclusion_policy.errors import (
    SUBJECT_FIELD_TYPE_INVALID,
    SUBJECT_ID_INVALID,
    SUBJECT_LOCATOR_NOT_NORMALIZED,
    SUBJECT_SIZE_INVALID,
    SUBJECT_WORKSPACE_MISMATCH,
    input_invalid,
)
from personal_os.exclusion_policy.normalization import (
    fold_ascii_lowercase,
    glob_matches,
    normalize_locator,
)
from personal_os.object_storage import CanonicalMediaType
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import SourceType

#: Which subject field each rule kind requires (spec 7).
_REQUIRED_FIELD_BY_KIND: Final[dict[RuleKind, PolicySubjectField]] = {
    RuleKind.EXACT_SOURCE_ID: PolicySubjectField.SOURCE_ID,
    RuleKind.FOLDER_PREFIX: PolicySubjectField.NORMALIZED_LOCATOR,
    RuleKind.PATH_GLOB: PolicySubjectField.NORMALIZED_LOCATOR,
    RuleKind.EXTENSION: PolicySubjectField.NORMALIZED_LOCATOR,
    RuleKind.MEDIA_TYPE: PolicySubjectField.MEDIA_TYPE,
    RuleKind.MAXIMUM_SIZE: PolicySubjectField.SIZE_BYTES,
    RuleKind.SOURCE_TYPE: PolicySubjectField.SOURCE_TYPE,
}


@dataclass(frozen=True, slots=True)
class PolicyEvaluationOutcome:
    """Deterministic evaluation evidence: closed decisions, IDs and fields.

    ``raw`` is the exact evaluator decision; ``enforced`` maps indeterminate
    to excluded. ``matched_rule_ids`` and ``missing_fields`` are sorted and
    contain no rule operand or subject locator, so the value is safe to retain
    as internal evidence.
    """

    raw: RawPolicyDecision
    enforced: EnforcedPolicyDecision
    matched_rule_ids: tuple[UUID, ...]
    missing_fields: tuple[PolicySubjectField, ...]


def _validate_subject(revision: ExclusionPolicyRevision, subject: PolicySubject) -> None:
    """Fail closed on invalid evidence: never invent or repair field values."""

    if subject.workspace_id != revision.workspace_id:
        raise input_invalid(SUBJECT_WORKSPACE_MISMATCH)
    if subject.source_id is not None:
        try:
            reject_nil_uuid("subject.source_id", subject.source_id)
        except ValueError:
            raise input_invalid(SUBJECT_ID_INVALID) from None
    if subject.normalized_locator is not None:
        if not isinstance(subject.normalized_locator, str):
            raise input_invalid(SUBJECT_FIELD_TYPE_INVALID)
        # Re-running normalization rejects malformed locators outright and
        # proves the caller passed the canonical NFC form (idempotency).
        if normalize_locator(subject.normalized_locator) != subject.normalized_locator:
            raise input_invalid(SUBJECT_LOCATOR_NOT_NORMALIZED)
    if subject.source_type is not None and not isinstance(subject.source_type, SourceType):
        raise input_invalid(SUBJECT_FIELD_TYPE_INVALID)
    if subject.media_type is not None and not isinstance(subject.media_type, CanonicalMediaType):
        raise input_invalid(SUBJECT_FIELD_TYPE_INVALID)
    if subject.size_bytes is not None:
        if isinstance(subject.size_bytes, bool) or not isinstance(subject.size_bytes, int):
            raise input_invalid(SUBJECT_SIZE_INVALID)
        if subject.size_bytes < 0:
            raise input_invalid(SUBJECT_SIZE_INVALID)


def _rule_matches(rule_operand: RuleOperand, subject: PolicySubject) -> bool | None:
    """Definite match, definite non-match, or ``None`` when evidence is absent."""

    if isinstance(rule_operand, ExactSourceIdOperand):
        if subject.source_id is None:
            return None
        return subject.source_id == rule_operand.source_id
    if isinstance(rule_operand, FolderPrefixOperand):
        if subject.normalized_locator is None:
            return None
        segments = subject.normalized_locator.split("/")
        prefix_segments = rule_operand.folder_prefix.split("/")
        return segments[: len(prefix_segments)] == prefix_segments
    if isinstance(rule_operand, PathGlobOperand):
        if subject.normalized_locator is None:
            return None
        return glob_matches(rule_operand.compiled, tuple(subject.normalized_locator.split("/")))
    if isinstance(rule_operand, ExtensionOperand):
        if subject.normalized_locator is None:
            return None
        final_filename = subject.normalized_locator.split("/")[-1]
        return fold_ascii_lowercase(final_filename).endswith(rule_operand.extension)
    if isinstance(rule_operand, MediaTypeOperand):
        if subject.media_type is None:
            return None
        if rule_operand.exact_media_type is not None:
            return subject.media_type.value == rule_operand.exact_media_type.value
        return subject.media_type.value.partition("/")[0] == rule_operand.family_type
    if isinstance(rule_operand, MaximumSizeOperand):
        if subject.size_bytes is None:
            return None
        return subject.size_bytes > rule_operand.maximum_size_bytes
    if isinstance(rule_operand, SourceTypeOperand):
        if subject.source_type is None:
            return None
        return subject.source_type is rule_operand.source_type
    return None


def evaluate_policy(
    *,
    revision: ExclusionPolicyRevision,
    subject: PolicySubject,
) -> PolicyEvaluationOutcome:
    """Evaluate one subject against one immutable revision, deterministically."""

    _validate_subject(revision, subject)

    matched_rule_ids: list[UUID] = []
    missing_fields: set[PolicySubjectField] = set()
    for rule in revision.rules:
        outcome = _rule_matches(rule.operand, subject)
        if outcome is None:
            missing_fields.add(_REQUIRED_FIELD_BY_KIND[rule.rule_kind])
        elif outcome:
            matched_rule_ids.append(rule.rule_id)

    if matched_rule_ids:
        raw = RawPolicyDecision.EXCLUDED
        enforced = EnforcedPolicyDecision.EXCLUDED
    elif missing_fields:
        raw = RawPolicyDecision.INDETERMINATE
        enforced = EnforcedPolicyDecision.EXCLUDED
    else:
        raw = RawPolicyDecision.ALLOWED
        enforced = EnforcedPolicyDecision.ALLOWED

    return PolicyEvaluationOutcome(
        raw=raw,
        enforced=enforced,
        matched_rule_ids=tuple(sorted(matched_rule_ids)),
        missing_fields=tuple(sorted(missing_fields, key=lambda field: field.value)),
    )
