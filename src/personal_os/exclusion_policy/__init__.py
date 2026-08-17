"""Framework-neutral exclusion-policy domain package.

Closed rule contracts and immutable revision/subject values, shared locator
and operand normalization with the bounded glob compiler, the pure deny-only
evaluator, the typed error bound to the closed ``exclusion_policy_*``
registry codes, and the low-cardinality evaluation metrics contracts. The
package imports no web framework, database driver, provider SDK or
composition root; it reuses the canonical ``SourceType`` and
``CanonicalMediaType`` value semantics.
"""

from personal_os.exclusion_policy.contracts import (
    EVALUATOR_CONTRACT,
    EXTENSION_MAXIMUM_CHARACTERS,
    EXTENSION_MINIMUM_CHARACTERS,
    GLOB_MAXIMUM_BYTES,
    GLOB_MAXIMUM_SEGMENTS,
    GLOB_MAXIMUM_WILDCARD_TOKENS,
    LOCATOR_MAXIMUM_BYTES,
    LOCATOR_MAXIMUM_SEGMENTS,
    LOCATOR_SEGMENT_MAXIMUM_BYTES,
    MAXIMUM_RULES_PER_REVISION,
    MAXIMUM_SIZE_BYTES_CEILING,
    CompiledGlob,
    EnforcedPolicyDecision,
    ExactSourceIdOperand,
    ExclusionPolicyRevision,
    ExclusionRule,
    ExtensionOperand,
    FolderPrefixOperand,
    GlobLiteralPart,
    GlobSegment,
    GlobSegmentPart,
    GlobStarPart,
    MaximumSizeOperand,
    MediaTypeOperand,
    PathGlobOperand,
    PolicySubject,
    PolicySubjectField,
    PreviewMatchState,
    RawPolicyDecision,
    RuleKind,
    RuleOperand,
    SourceTypeOperand,
    preview_match_state,
)
from personal_os.exclusion_policy.errors import (
    INPUT_REASONS,
    ExclusionPolicyError,
)
from personal_os.exclusion_policy.evaluation import (
    PolicyEvaluationOutcome,
    evaluate_policy,
)
from personal_os.exclusion_policy.metrics import (
    EXCLUSION_POLICY_METRIC_CONTRACTS,
    EvaluationMetricOutcome,
    EvaluationRecord,
    ExclusionPolicyMetrics,
    InMemoryExclusionPolicyMetrics,
    PolicyBoundary,
)
from personal_os.exclusion_policy.normalization import (
    RULE_FINGERPRINT_CONTRACT,
    compile_glob,
    fold_ascii_lowercase,
    glob_matches,
    normalize_locator,
    normalize_rule,
)

__all__ = [
    "EVALUATOR_CONTRACT",
    "EXCLUSION_POLICY_METRIC_CONTRACTS",
    "EXTENSION_MAXIMUM_CHARACTERS",
    "EXTENSION_MINIMUM_CHARACTERS",
    "GLOB_MAXIMUM_BYTES",
    "GLOB_MAXIMUM_SEGMENTS",
    "GLOB_MAXIMUM_WILDCARD_TOKENS",
    "INPUT_REASONS",
    "LOCATOR_MAXIMUM_BYTES",
    "LOCATOR_MAXIMUM_SEGMENTS",
    "LOCATOR_SEGMENT_MAXIMUM_BYTES",
    "MAXIMUM_RULES_PER_REVISION",
    "MAXIMUM_SIZE_BYTES_CEILING",
    "RULE_FINGERPRINT_CONTRACT",
    "CompiledGlob",
    "EnforcedPolicyDecision",
    "EvaluationMetricOutcome",
    "EvaluationRecord",
    "ExactSourceIdOperand",
    "ExclusionPolicyError",
    "ExclusionPolicyMetrics",
    "ExclusionPolicyRevision",
    "ExclusionRule",
    "ExtensionOperand",
    "FolderPrefixOperand",
    "GlobLiteralPart",
    "GlobSegment",
    "GlobSegmentPart",
    "GlobStarPart",
    "InMemoryExclusionPolicyMetrics",
    "MaximumSizeOperand",
    "MediaTypeOperand",
    "PathGlobOperand",
    "PolicyBoundary",
    "PolicyEvaluationOutcome",
    "PolicySubject",
    "PolicySubjectField",
    "PreviewMatchState",
    "RawPolicyDecision",
    "RuleKind",
    "RuleOperand",
    "SourceTypeOperand",
    "compile_glob",
    "evaluate_policy",
    "fold_ascii_lowercase",
    "glob_matches",
    "normalize_locator",
    "normalize_rule",
    "preview_match_state",
]
