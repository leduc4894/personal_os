"""Deny-only evaluation: the truth table, precedence and evidence discipline.

Proves default allow, any-definite-match exclusion, missing-field
indeterminacy with enforced deny, definite-match-over-missing precedence,
every rule kind's boundary behavior (folder segment boundaries, glob
anchoring, ASCII-insensitive extension, media families, size equality),
subject validation, the 256-rule ceiling and that outcome evidence never
carries locators or operands.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from tests.unit.exclusion_policy.fakes import (
    WORKSPACE_ID,
    extension_rule,
    maximum_size_rule,
    revision,
    rule,
    subject,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    ExclusionPolicyRevision,
    PolicySubject,
    PolicySubjectField,
    RawPolicyDecision,
    RuleKind,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.evaluation import evaluate_policy
from personal_os.exclusion_policy.normalization import normalize_locator
from personal_os.object_storage import CanonicalMediaType
from personal_os.sources.commands import SourceType

SUBJECT_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-0000000000a1")
OTHER_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-0000000000a2")


def _reason_of(error: ExclusionPolicyError) -> str:
    return str(error.safe_details["reason"])


def test_missing_size_is_indeterminate_for_maximum_size() -> None:
    decision = evaluate_policy(
        revision=revision(rule(RuleKind.MAXIMUM_SIZE, size_bytes_operand=8 * 1024 * 1024)),
        subject=subject(size_bytes=None),
    )
    assert decision.raw is RawPolicyDecision.INDETERMINATE
    assert decision.enforced is EnforcedPolicyDecision.EXCLUDED


def test_any_definite_match_excludes_even_when_another_rule_is_indeterminate() -> None:
    decision = evaluate_policy(
        revision=revision(extension_rule(".pdf"), maximum_size_rule(1024)),
        subject=subject(normalized_locator="vault/a.pdf", size_bytes=None),
    )
    assert decision.raw is RawPolicyDecision.EXCLUDED


def test_empty_revision_allows_subject_with_no_optional_fields() -> None:
    decision = evaluate_policy(revision=revision(), subject=subject())
    assert decision.raw is RawPolicyDecision.ALLOWED
    assert decision.enforced is EnforcedPolicyDecision.ALLOWED
    assert decision.matched_rule_ids == ()
    assert decision.missing_fields == ()


def test_enforced_decision_maps_indeterminate_to_deny() -> None:
    indeterminate = evaluate_policy(
        revision=revision(maximum_size_rule(1024)), subject=subject(size_bytes=None)
    )
    excluded = evaluate_policy(
        revision=revision(extension_rule(".pdf")),
        subject=subject(normalized_locator="a.pdf"),
    )
    allowed = evaluate_policy(revision=revision(), subject=subject())
    assert indeterminate.raw is RawPolicyDecision.INDETERMINATE
    assert indeterminate.enforced is EnforcedPolicyDecision.EXCLUDED
    assert excluded.raw is RawPolicyDecision.EXCLUDED
    assert excluded.enforced is EnforcedPolicyDecision.EXCLUDED
    assert allowed.raw is RawPolicyDecision.ALLOWED
    assert allowed.enforced is EnforcedPolicyDecision.ALLOWED


def test_exact_source_id_matches_equal_id_and_reports_missing_field() -> None:
    matched = evaluate_policy(
        revision=revision(rule(RuleKind.EXACT_SOURCE_ID, source_id_operand=SUBJECT_SOURCE_ID)),
        subject=subject(source_id=SUBJECT_SOURCE_ID),
    )
    assert matched.raw is RawPolicyDecision.EXCLUDED
    assert matched.enforced is EnforcedPolicyDecision.EXCLUDED
    assert matched.missing_fields == ()
    different = evaluate_policy(
        revision=revision(rule(RuleKind.EXACT_SOURCE_ID, source_id_operand=OTHER_SOURCE_ID)),
        subject=subject(source_id=SUBJECT_SOURCE_ID),
    )
    assert different.raw is RawPolicyDecision.ALLOWED
    missing = evaluate_policy(
        revision=revision(rule(RuleKind.EXACT_SOURCE_ID, source_id_operand=SUBJECT_SOURCE_ID)),
        subject=subject(),
    )
    assert missing.raw is RawPolicyDecision.INDETERMINATE
    assert missing.missing_fields == (PolicySubjectField.SOURCE_ID,)
    assert missing.matched_rule_ids == ()


def test_folder_prefix_matches_only_at_segment_boundaries() -> None:
    policy = revision(rule(RuleKind.FOLDER_PREFIX, text_operand="private"))
    for locator in ("private/a.md", "private/b/c.md", "private"):
        decision = evaluate_policy(revision=policy, subject=subject(normalized_locator=locator))
        assert decision.raw is RawPolicyDecision.EXCLUDED, locator
    for locator in ("private-notes/a.md", "notes/private/a.md", "privat/a.md"):
        decision = evaluate_policy(revision=policy, subject=subject(normalized_locator=locator))
        assert decision.raw is RawPolicyDecision.ALLOWED, locator
    missing = evaluate_policy(revision=policy, subject=subject())
    assert missing.missing_fields == (PolicySubjectField.NORMALIZED_LOCATOR,)


def test_extension_matches_final_filename_ascii_case_insensitively() -> None:
    policy = revision(extension_rule(".pdf"))
    for locator in ("a.pdf", "NOTES/a.PDF", "archive.tar.pdf", "a.PdF"):
        decision = evaluate_policy(revision=policy, subject=subject(normalized_locator=locator))
        assert decision.raw is RawPolicyDecision.EXCLUDED, locator
    for locator in ("a.pdfx", "pd f/a.md", "a.md"):
        decision = evaluate_policy(revision=policy, subject=subject(normalized_locator=locator))
        assert decision.raw is RawPolicyDecision.ALLOWED, locator


def test_path_matching_is_case_sensitive_outside_extension_rules() -> None:
    policy = revision(rule(RuleKind.PATH_GLOB, text_operand="Notes/*.md"))
    included = evaluate_policy(revision=policy, subject=subject(normalized_locator="Notes/a.md"))
    excluded_case = evaluate_policy(
        revision=policy, subject=subject(normalized_locator="notes/a.md")
    )
    assert included.raw is RawPolicyDecision.EXCLUDED
    assert excluded_case.raw is RawPolicyDecision.ALLOWED


def test_glob_is_anchored_to_the_whole_normalized_locator() -> None:
    single_star = revision(rule(RuleKind.PATH_GLOB, text_operand="*.md"))
    assert (
        evaluate_policy(revision=single_star, subject=subject(normalized_locator="a.md")).raw
        is RawPolicyDecision.EXCLUDED
    )
    assert (
        evaluate_policy(revision=single_star, subject=subject(normalized_locator="sub/a.md")).raw
        is RawPolicyDecision.ALLOWED
    )
    double_star = revision(rule(RuleKind.PATH_GLOB, text_operand="**/*.md"))
    for locator in ("a.md", "sub/a.md", "deep/x/y.md"):
        assert (
            evaluate_policy(revision=double_star, subject=subject(normalized_locator=locator)).raw
            is RawPolicyDecision.EXCLUDED
        ), locator
    zero_segment = revision(rule(RuleKind.PATH_GLOB, text_operand="a/**/b"))
    assert (
        evaluate_policy(revision=zero_segment, subject=subject(normalized_locator="a/b")).raw
        is RawPolicyDecision.EXCLUDED
    )
    everything = revision(rule(RuleKind.PATH_GLOB, text_operand="**"))
    assert (
        evaluate_policy(revision=everything, subject=subject(normalized_locator="x/y/z.md")).raw
        is RawPolicyDecision.EXCLUDED
    )


def test_media_type_matches_exact_value_or_top_level_family() -> None:
    family = revision(rule(RuleKind.MEDIA_TYPE, text_operand="text/*"))
    assert (
        evaluate_policy(
            revision=family, subject=subject(media_type=CanonicalMediaType.parse("text/markdown"))
        ).raw
        is RawPolicyDecision.EXCLUDED
    )
    assert (
        evaluate_policy(
            revision=family,
            subject=subject(media_type=CanonicalMediaType.parse("application/pdf")),
        ).raw
        is RawPolicyDecision.ALLOWED
    )
    exact = revision(rule(RuleKind.MEDIA_TYPE, text_operand="application/pdf"))
    assert (
        evaluate_policy(
            revision=exact, subject=subject(media_type=CanonicalMediaType.parse("application/pdf"))
        ).raw
        is RawPolicyDecision.EXCLUDED
    )
    assert (
        evaluate_policy(
            revision=exact, subject=subject(media_type=CanonicalMediaType.parse("text/markdown"))
        ).raw
        is RawPolicyDecision.ALLOWED
    )
    missing = evaluate_policy(revision=family, subject=subject())
    assert missing.missing_fields == (PolicySubjectField.MEDIA_TYPE,)


def test_source_type_matches_the_closed_enum_value() -> None:
    policy = revision(rule(RuleKind.SOURCE_TYPE, text_operand="markdown"))
    matched = evaluate_policy(revision=policy, subject=subject(source_type=SourceType.MARKDOWN))
    other = evaluate_policy(revision=policy, subject=subject(source_type=SourceType.PDF))
    missing = evaluate_policy(revision=policy, subject=subject())
    assert matched.raw is RawPolicyDecision.EXCLUDED
    assert other.raw is RawPolicyDecision.ALLOWED
    assert missing.raw is RawPolicyDecision.INDETERMINATE
    assert missing.missing_fields == (PolicySubjectField.SOURCE_TYPE,)


def test_maximum_size_excludes_only_above_the_bound() -> None:
    policy = revision(maximum_size_rule(1024))
    at_bound = evaluate_policy(revision=policy, subject=subject(size_bytes=1024))
    above_bound = evaluate_policy(revision=policy, subject=subject(size_bytes=1025))
    below_bound = evaluate_policy(revision=policy, subject=subject(size_bytes=0))
    assert at_bound.raw is RawPolicyDecision.ALLOWED
    assert above_bound.raw is RawPolicyDecision.EXCLUDED
    assert below_bound.raw is RawPolicyDecision.ALLOWED


def test_maximum_size_zero_excludes_every_non_empty_subject_only() -> None:
    policy = revision(maximum_size_rule(0))
    empty = evaluate_policy(revision=policy, subject=subject(size_bytes=0))
    non_empty = evaluate_policy(revision=policy, subject=subject(size_bytes=1))
    assert empty.raw is RawPolicyDecision.ALLOWED
    assert non_empty.raw is RawPolicyDecision.EXCLUDED


def test_multiple_missing_fields_are_reported_sorted() -> None:
    policy = revision(
        rule(RuleKind.SOURCE_TYPE, text_operand="markdown"),
        rule(RuleKind.MEDIA_TYPE, text_operand="text/*"),
        maximum_size_rule(10),
    )
    decision = evaluate_policy(revision=policy, subject=subject())
    assert decision.raw is RawPolicyDecision.INDETERMINATE
    assert decision.missing_fields == (
        PolicySubjectField.MEDIA_TYPE,
        PolicySubjectField.SIZE_BYTES,
        PolicySubjectField.SOURCE_TYPE,
    )


def test_matched_rule_ids_are_sorted_and_complete() -> None:
    first = uuid4()
    second = uuid4()
    third = uuid4()
    policy = revision(
        rule(RuleKind.EXTENSION, text_operand=".pdf", rule_id=first),
        rule(RuleKind.PATH_GLOB, text_operand="**/*.pdf", rule_id=second),
        rule(RuleKind.FOLDER_PREFIX, text_operand="vault", rule_id=third),
        rule(RuleKind.EXTENSION, text_operand=".md"),
    )
    decision = evaluate_policy(revision=policy, subject=subject(normalized_locator="vault/a.pdf"))
    assert decision.matched_rule_ids == tuple(sorted((first, second, third)))
    assert decision.raw is RawPolicyDecision.EXCLUDED


def test_evaluation_is_deterministic_for_equal_inputs() -> None:
    policy = revision(extension_rule(".pdf"), maximum_size_rule(10))
    first = evaluate_policy(
        revision=policy, subject=subject(normalized_locator="vault/a.pdf", size_bytes=None)
    )
    second = evaluate_policy(
        revision=policy, subject=subject(normalized_locator="vault/a.pdf", size_bytes=None)
    )
    assert first == second


def test_evaluate_policy_rejects_revision_with_too_many_rules_before_evaluation() -> None:
    rules = tuple(rule(RuleKind.EXTENSION, text_operand=f".e{i:03d}") for i in range(257))
    with pytest.raises(ValueError, match="at most 256 rules"):
        ExclusionPolicyRevision(
            policy_revision_id=uuid4(),
            workspace_id=WORKSPACE_ID,
            revision_number=1,
            rules=rules,
        )


def test_subject_validation_rejects_unnormalized_and_invalid_fields() -> None:
    policy = revision(extension_rule(".pdf"))
    with pytest.raises(ExclusionPolicyError) as not_normalized:
        evaluate_policy(revision=policy, subject=subject(normalized_locator="caf\u0065\u0301/a.md"))
    assert _reason_of(not_normalized.value) == "subject_locator_not_normalized"
    with pytest.raises(ExclusionPolicyError) as trailing:
        evaluate_policy(revision=policy, subject=subject(normalized_locator="notes/"))
    assert _reason_of(trailing.value) == "locator_trailing_separator"
    with pytest.raises(ExclusionPolicyError) as invalid_locator:
        evaluate_policy(revision=policy, subject=subject(normalized_locator="notes\\a.md"))
    assert _reason_of(invalid_locator.value) == "locator_backslash_separator"
    with pytest.raises(ExclusionPolicyError) as negative_size:
        evaluate_policy(revision=policy, subject=subject(size_bytes=-1))
    assert _reason_of(negative_size.value) == "subject_size_invalid"
    with pytest.raises(ExclusionPolicyError) as boolean_size:
        evaluate_policy(revision=policy, subject=subject(size_bytes=True))  # type: ignore[arg-type]
    assert _reason_of(boolean_size.value) == "subject_size_invalid"
    with pytest.raises(ExclusionPolicyError) as nil_source_id:
        evaluate_policy(revision=policy, subject=subject(source_id=UUID(int=0)))
    assert _reason_of(nil_source_id.value) == "subject_id_invalid"
    with pytest.raises(ExclusionPolicyError) as wrong_media_type:
        evaluate_policy(
            revision=policy,
            subject=subject(media_type="text/plain"),  # type: ignore[arg-type]
        )
    assert _reason_of(wrong_media_type.value) == "subject_field_type_invalid"
    with pytest.raises(ExclusionPolicyError) as wrong_source_type:
        evaluate_policy(
            revision=policy,
            subject=subject(source_type="markdown"),  # type: ignore[arg-type]
        )
    assert _reason_of(wrong_source_type.value) == "subject_field_type_invalid"


def test_subject_workspace_must_match_revision_workspace() -> None:
    cross_workspace_subject = PolicySubject(workspace_id=uuid4())
    with pytest.raises(ExclusionPolicyError) as error_info:
        evaluate_policy(revision=revision(extension_rule(".pdf")), subject=cross_workspace_subject)
    assert error_info.value.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
    assert _reason_of(error_info.value) == "subject_workspace_mismatch"


def test_outcome_evidence_never_contains_locators_or_operands() -> None:
    policy = revision(
        extension_rule(".pdf"),
        rule(RuleKind.PATH_GLOB, text_operand="**/*.pdf"),
        maximum_size_rule(4096),
    )
    decision = evaluate_policy(revision=policy, subject=subject(normalized_locator="vault/a.pdf"))
    rendered = repr(decision) + str(decision)
    for sensitive in ("vault", "a.pdf", ".pdf", "**", "4096"):
        assert sensitive not in rendered
    assert decision.matched_rule_ids  # evidence is the sorted UUIDs only
    for rule_id in decision.matched_rule_ids:
        assert isinstance(rule_id, UUID)


def test_nfc_equivalent_locators_evaluate_identically() -> None:
    policy = revision(rule(RuleKind.FOLDER_PREFIX, text_operand="caf\u00e9"))
    composed = evaluate_policy(
        revision=policy, subject=subject(normalized_locator="caf\u00e9/a.md")
    )
    decomposed = evaluate_policy(
        revision=policy,
        subject=subject(normalized_locator=normalize_locator("cafe\u0301/a.md")),
    )
    assert composed.raw is RawPolicyDecision.EXCLUDED
    assert decomposed.raw is RawPolicyDecision.EXCLUDED
    assert decomposed.matched_rule_ids == composed.matched_rule_ids
