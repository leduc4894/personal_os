"""Closed rule contracts: enums, operand mapping invariants and the error registry.

Pins the seven rule kinds, the three raw and two enforced decisions, the
preview match vocabulary and the five subject fields; proves the frozen value
objects reject wrong operand kinds, nil IDs, oversized revisions and duplicate
semantic fingerprints; and pins the thirteen spec 19 error-code registry
definitions (category, retryability, safe detail fields) that this task adds.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from tests.unit.exclusion_policy.fakes import (
    POLICY_REVISION_ID,
    WORKSPACE_ID,
    rule,
)

from personal_os.error_contracts.codes import ERROR_DEFINITIONS, ErrorCategory, ErrorCode
from personal_os.exclusion_policy.contracts import (
    MAXIMUM_RULES_PER_REVISION,
    EnforcedPolicyDecision,
    ExclusionPolicyRevision,
    ExclusionRule,
    ExtensionOperand,
    MaximumSizeOperand,
    MediaTypeOperand,
    PolicySubject,
    PolicySubjectField,
    PreviewMatchState,
    RawPolicyDecision,
    RuleKind,
    preview_match_state,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError

_FINGERPRINT = "a" * 64


def test_rule_kind_is_the_closed_seven_member_vocabulary() -> None:
    assert {kind.value for kind in RuleKind} == {
        "exact_source_id",
        "folder_prefix",
        "path_glob",
        "extension",
        "media_type",
        "maximum_size",
        "source_type",
    }


def test_decision_and_match_state_vocabularies_are_closed() -> None:
    assert {decision.value for decision in RawPolicyDecision} == {
        "allowed",
        "excluded",
        "indeterminate",
    }
    assert {decision.value for decision in EnforcedPolicyDecision} == {
        "allowed",
        "excluded",
    }
    assert {state.value for state in PreviewMatchState} == {
        "matched",
        "not_matched",
        "indeterminate",
    }
    assert {field.value for field in PolicySubjectField} == {
        "source_id",
        "normalized_locator",
        "source_type",
        "media_type",
        "size_bytes",
    }


def test_preview_match_state_maps_the_raw_decision_vocabulary() -> None:
    assert preview_match_state(RawPolicyDecision.EXCLUDED) is PreviewMatchState.MATCHED
    assert preview_match_state(RawPolicyDecision.ALLOWED) is PreviewMatchState.NOT_MATCHED
    assert preview_match_state(RawPolicyDecision.INDETERMINATE) is PreviewMatchState.INDETERMINATE


def test_rule_rejects_operand_kind_that_does_not_match_rule_kind() -> None:
    with pytest.raises(ValueError, match="operand does not match rule kind"):
        ExclusionRule(
            rule_id=uuid4(),
            rule_kind=RuleKind.EXTENSION,
            operand=MaximumSizeOperand(maximum_size_bytes=1024),
            semantic_fingerprint=_FINGERPRINT,
        )


def test_rule_rejects_nil_rule_id_and_malformed_fingerprint() -> None:
    with pytest.raises(ValueError, match="rule_id must be a non-nil UUID"):
        ExclusionRule(
            rule_id=UUID(int=0),
            rule_kind=RuleKind.MAXIMUM_SIZE,
            operand=MaximumSizeOperand(maximum_size_bytes=1),
            semantic_fingerprint=_FINGERPRINT,
        )
    with pytest.raises(ValueError, match="semantic fingerprint"):
        ExclusionRule(
            rule_id=uuid4(),
            rule_kind=RuleKind.MAXIMUM_SIZE,
            operand=MaximumSizeOperand(maximum_size_bytes=1),
            semantic_fingerprint="XYZ",
        )


def test_media_type_operand_holds_exactly_one_of_exact_or_family() -> None:
    with pytest.raises(ValueError, match="exactly one media type operand"):
        MediaTypeOperand(exact_media_type=None, family_type=None)


def test_subject_rejects_nil_workspace_id() -> None:
    with pytest.raises(ValueError, match="workspace_id must be a non-nil UUID"):
        PolicySubject(workspace_id=UUID(int=0))


def test_revision_rejects_nil_ids_and_non_positive_revision_number() -> None:
    with pytest.raises(ValueError, match="policy_revision_id must be a non-nil UUID"):
        ExclusionPolicyRevision(
            policy_revision_id=UUID(int=0),
            workspace_id=WORKSPACE_ID,
            revision_number=1,
        )
    with pytest.raises(ValueError, match="workspace_id must be a non-nil UUID"):
        ExclusionPolicyRevision(
            policy_revision_id=POLICY_REVISION_ID,
            workspace_id=UUID(int=0),
            revision_number=1,
        )
    with pytest.raises(ValueError, match="revision_number"):
        ExclusionPolicyRevision(
            policy_revision_id=POLICY_REVISION_ID,
            workspace_id=WORKSPACE_ID,
            revision_number=0,
        )


def test_revision_rejects_more_than_the_maximum_rule_count() -> None:
    rules = tuple(rule(RuleKind.EXTENSION, text_operand=f".e{i:03d}") for i in range(257))
    with pytest.raises(ValueError, match="at most 256 rules"):
        ExclusionPolicyRevision(
            policy_revision_id=POLICY_REVISION_ID,
            workspace_id=WORKSPACE_ID,
            revision_number=1,
            rules=rules,
        )


def test_revision_accepts_exactly_the_maximum_rule_count() -> None:
    rules = tuple(rule(RuleKind.EXTENSION, text_operand=f".e{i:03d}") for i in range(256))
    revision = ExclusionPolicyRevision(
        policy_revision_id=POLICY_REVISION_ID,
        workspace_id=WORKSPACE_ID,
        revision_number=1,
        rules=rules,
    )
    assert len(revision.rules) == MAXIMUM_RULES_PER_REVISION


def test_revision_rejects_duplicate_semantic_fingerprints_and_rule_ids() -> None:
    duplicate_semantics = (
        rule(RuleKind.EXTENSION, text_operand=".pdf"),
        rule(RuleKind.EXTENSION, text_operand=".PDF"),
    )
    with pytest.raises(ValueError, match="duplicate semantic fingerprint"):
        ExclusionPolicyRevision(
            policy_revision_id=POLICY_REVISION_ID,
            workspace_id=WORKSPACE_ID,
            revision_number=1,
            rules=duplicate_semantics,
        )
    shared_rule_id = uuid4()
    duplicate_ids = (
        rule(RuleKind.EXTENSION, text_operand=".pdf", rule_id=shared_rule_id),
        rule(RuleKind.EXTENSION, text_operand=".md", rule_id=shared_rule_id),
    )
    with pytest.raises(ValueError, match="duplicate rule_id"):
        ExclusionPolicyRevision(
            policy_revision_id=POLICY_REVISION_ID,
            workspace_id=WORKSPACE_ID,
            revision_number=1,
            rules=duplicate_ids,
        )


#: Exact spec section 19 mapping: (category, is_retryable, allowed detail fields).
SPEC_NINETEEN_REGISTRY_CONTRACT = {
    ErrorCode.EXCLUSION_POLICY_INPUT_INVALID: (
        ErrorCategory.VALIDATION,
        False,
        frozenset({"reason", "rule_index"}),
    ),
    ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED: (ErrorCategory.CONFLICT, False, frozenset()),
    ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT: (
        ErrorCategory.CONFLICT,
        False,
        frozenset({"current_draft_version"}),
    ),
    ErrorCode.EXCLUSION_POLICY_PREVIEW_PENDING: (
        ErrorCategory.CONFLICT,
        True,
        frozenset({"retry_after_seconds"}),
    ),
    ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED: (
        ErrorCategory.CONFLICT,
        False,
        frozenset({"reason"}),
    ),
    ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED: (ErrorCategory.CONFLICT, False, frozenset()),
    ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE: (
        ErrorCategory.CONFLICT,
        False,
        frozenset({"reason"}),
    ),
    ErrorCode.EXCLUSION_POLICY_CONFIRMATION_INVALID: (
        ErrorCategory.CONFLICT,
        False,
        frozenset(),
    ),
    ErrorCode.EXCLUSION_POLICY_DENIED: (
        ErrorCategory.AUTHORIZATION,
        False,
        frozenset({"policy_revision_number"}),
    ),
    ErrorCode.EXCLUSION_POLICY_INDETERMINATE: (
        ErrorCategory.AUTHORIZATION,
        False,
        frozenset({"reason"}),
    ),
    ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED: (
        ErrorCategory.CONFLICT,
        True,
        frozenset({"current_policy_revision_number"}),
    ),
    ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE: (
        ErrorCategory.DEPENDENCY,
        False,
        frozenset(),
    ),
    ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN: (
        ErrorCategory.DEPENDENCY,
        True,
        frozenset(),
    ),
}


def test_exclusion_policy_codes_match_spec_nineteen_registry_contract() -> None:
    assert len(SPEC_NINETEEN_REGISTRY_CONTRACT) == 13
    for error_code, (
        category,
        is_retryable,
        allowed_fields,
    ) in SPEC_NINETEEN_REGISTRY_CONTRACT.items():
        definition = ERROR_DEFINITIONS[error_code]
        assert definition.category is category, error_code
        assert definition.is_retryable is is_retryable, error_code
        assert definition.allowed_detail_fields == allowed_fields, error_code
        assert definition.safe_message, error_code


def test_exclusion_policy_error_accepts_only_the_closed_code_set() -> None:
    error = ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
    assert error.to_safe_dict()["error_code"] == "exclusion_policy_not_initialized"
    with pytest.raises(ValueError, match="not valid for this exception type"):
        ExclusionPolicyError(ErrorCode.CONFIGURATION_INVALID)


def test_operand_values_are_immutable() -> None:
    operand = ExtensionOperand(extension=".pdf")
    with pytest.raises(AttributeError):
        operand.extension = ".md"  # type: ignore[misc]
