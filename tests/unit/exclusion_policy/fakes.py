"""Deterministic builders for the exclusion-policy evaluator unit tests.

The builders mirror the three normalized operand columns a draft rule carries
(``source_id_operand``, ``text_operand``, ``size_bytes_operand``) and route
every rule through :func:`personal_os.exclusion_policy.normalization.normalize_rule`
so tests only ever see normalized, fingerprinted domain values.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from personal_os.exclusion_policy.contracts import (
    ExclusionPolicyRevision,
    ExclusionRule,
    PolicySubject,
    RuleKind,
)
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.object_storage import CanonicalMediaType
from personal_os.sources.commands import SourceType

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-000000000001")
POLICY_REVISION_ID = UUID("018f47a0-7b00-7000-8000-0000000000e1")


def rule(
    rule_kind: RuleKind,
    *,
    rule_id: UUID | None = None,
    source_id_operand: UUID | None = None,
    text_operand: str | None = None,
    size_bytes_operand: int | None = None,
    rule_index: int | None = None,
) -> ExclusionRule:
    """Normalize one rule with a random stable rule ID unless overridden."""

    return normalize_rule(
        rule_id if rule_id is not None else uuid4(),
        rule_kind,
        source_id_operand=source_id_operand,
        text_operand=text_operand,
        size_bytes_operand=size_bytes_operand,
        rule_index=rule_index,
    )


def extension_rule(text_operand: str) -> ExclusionRule:
    return rule(RuleKind.EXTENSION, text_operand=text_operand)


def maximum_size_rule(size_bytes_operand: int) -> ExclusionRule:
    return rule(RuleKind.MAXIMUM_SIZE, size_bytes_operand=size_bytes_operand)


def revision(*rules: ExclusionRule, revision_number: int = 1) -> ExclusionPolicyRevision:
    return ExclusionPolicyRevision(
        policy_revision_id=POLICY_REVISION_ID,
        workspace_id=WORKSPACE_ID,
        revision_number=revision_number,
        rules=rules,
    )


def subject(
    *,
    source_id: UUID | None = None,
    normalized_locator: str | None = None,
    source_type: SourceType | None = None,
    media_type: CanonicalMediaType | None = None,
    size_bytes: int | None = None,
) -> PolicySubject:
    return PolicySubject(
        workspace_id=WORKSPACE_ID,
        source_id=source_id,
        normalized_locator=normalized_locator,
        source_type=source_type,
        media_type=media_type,
        size_bytes=size_bytes,
    )
