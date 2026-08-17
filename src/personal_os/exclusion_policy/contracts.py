"""Closed exclusion-policy value contracts: kinds, operands, rules and subjects.

These are the transport-neutral, immutable values shared by the backend and
the TypeScript evaluator. Every rule carries exactly one typed operand for its
closed rule kind, a lowercase SHA-256 semantic fingerprint over kind plus
normalized operand, and no name, description, priority, enabled flag or
action. Locators and operands are normalized values: construction sites must
route them through
:mod:`personal_os.exclusion_policy.normalization`; the frozen invariants here
then reject wrong operand kinds, nil IDs, oversized revisions and duplicate
semantics so the evaluator can never see an ambiguous rule set.

The module reuses the canonical ``SourceType`` and ``CanonicalMediaType``
value semantics; it creates no parallel provider-specific types and imports
no infrastructure SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from personal_os.object_storage import CanonicalMediaType
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import SourceType

#: Contract tag identifying the evaluator semantics across languages.
EVALUATOR_CONTRACT: Final[str] = "exclusion_policy_evaluator/v1"

#: Maximum rules in one draft or published revision (spec 6.1).
MAXIMUM_RULES_PER_REVISION: Final[int] = 256

#: Locator bounds (spec 6.3): 4,096 UTF-8 bytes, 256 segments, 255 bytes each.
LOCATOR_MAXIMUM_BYTES: Final[int] = 4096
LOCATOR_MAXIMUM_SEGMENTS: Final[int] = 256
LOCATOR_SEGMENT_MAXIMUM_BYTES: Final[int] = 255

#: Glob bounds (spec 6.4): 1,024 UTF-8 bytes, 64 segments, 16 wildcard tokens.
GLOB_MAXIMUM_BYTES: Final[int] = 1024
GLOB_MAXIMUM_SEGMENTS: Final[int] = 64
GLOB_MAXIMUM_WILDCARD_TOKENS: Final[int] = 16

#: Extension operand bounds (spec 6.2): 2-64 ASCII characters.
EXTENSION_MINIMUM_CHARACTERS: Final[int] = 2
EXTENSION_MAXIMUM_CHARACTERS: Final[int] = 64

#: Phase 2 maximum object size; ``maximum_size`` operands are inclusive in
#: ``0..104857600`` (spec 6.2).
MAXIMUM_SIZE_BYTES_CEILING: Final[int] = 104857600

_FINGERPRINT_HEX_LENGTH: Final[int] = 64
_HEX_LOWER: Final[frozenset[str]] = frozenset("0123456789abcdef")


class RuleKind(StrEnum):
    """Closed vocabulary of the seven deny-only rule kinds (spec 6.2)."""

    EXACT_SOURCE_ID = "exact_source_id"
    FOLDER_PREFIX = "folder_prefix"
    PATH_GLOB = "path_glob"
    EXTENSION = "extension"
    MEDIA_TYPE = "media_type"
    MAXIMUM_SIZE = "maximum_size"
    SOURCE_TYPE = "source_type"


class RawPolicyDecision(StrEnum):
    """Exact evaluator outcome before enforcement maps indeterminacy to deny."""

    ALLOWED = "allowed"
    EXCLUDED = "excluded"
    INDETERMINATE = "indeterminate"


class EnforcedPolicyDecision(StrEnum):
    """Product-level binary decision; indeterminate raw maps to excluded."""

    ALLOWED = "allowed"
    EXCLUDED = "excluded"


class PreviewMatchState(StrEnum):
    """Closed preview vocabulary mapping to proposed raw decisions (spec 10)."""

    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    INDETERMINATE = "indeterminate"


class PolicySubjectField(StrEnum):
    """Canonical subject fields rules can require (spec 7)."""

    SOURCE_ID = "source_id"
    NORMALIZED_LOCATOR = "normalized_locator"
    SOURCE_TYPE = "source_type"
    MEDIA_TYPE = "media_type"
    SIZE_BYTES = "size_bytes"


def preview_match_state(raw: RawPolicyDecision) -> PreviewMatchState:
    """Map one raw decision onto the closed preview match vocabulary."""

    if raw is RawPolicyDecision.EXCLUDED:
        return PreviewMatchState.MATCHED
    if raw is RawPolicyDecision.ALLOWED:
        return PreviewMatchState.NOT_MATCHED
    return PreviewMatchState.INDETERMINATE


@dataclass(frozen=True, slots=True)
class GlobStarPart:
    """One ``*`` wildcard matching zero or more code points inside a segment."""


@dataclass(frozen=True, slots=True)
class GlobLiteralPart:
    """One literal run inside a glob segment; never contains ``*``."""

    text: str


type GlobSegmentPart = GlobStarPart | GlobLiteralPart


@dataclass(frozen=True, slots=True)
class GlobSegment:
    """One compiled glob segment: a complete ``**`` or a bounded part sequence.

    ``is_double_star`` marks the single case where ``**`` is the complete
    segment and therefore matches zero or more whole path segments; any other
    ``*`` occurrence is an in-segment wildcard.
    """

    is_double_star: bool
    parts: tuple[GlobSegmentPart, ...]


@dataclass(frozen=True, slots=True)
class CompiledGlob:
    """Bounded token sequence compiled from one normalized glob operand.

    The evaluator matches against this structure directly; untrusted patterns
    are never translated into a backtracking regular expression.
    """

    segments: tuple[GlobSegment, ...]
    wildcard_token_count: int


@dataclass(frozen=True, slots=True)
class ExactSourceIdOperand:
    """Non-nil canonical source ID excluded exactly (spec 6.2)."""

    source_id: UUID


@dataclass(frozen=True, slots=True)
class FolderPrefixOperand:
    """Normalized relative folder matched at exact segment boundaries."""

    folder_prefix: str


@dataclass(frozen=True, slots=True)
class PathGlobOperand:
    """Normalized glob pattern plus its compiled bounded token sequence."""

    normalized_pattern: str
    compiled: CompiledGlob


@dataclass(frozen=True, slots=True)
class ExtensionOperand:
    """Lowercase ASCII suffix beginning with one dot (spec 6.2)."""

    extension: str


@dataclass(frozen=True, slots=True)
class MediaTypeOperand:
    """Exact canonical MIME value or one ``type/*`` top-level family."""

    exact_media_type: CanonicalMediaType | None
    family_type: str | None

    def __post_init__(self) -> None:
        if (self.exact_media_type is None) == (self.family_type is None):
            raise ValueError("exactly one media type operand member must be populated")


@dataclass(frozen=True, slots=True)
class MaximumSizeOperand:
    """Inclusive byte ceiling; excludes only strictly larger subjects."""

    maximum_size_bytes: int


@dataclass(frozen=True, slots=True)
class SourceTypeOperand:
    """Closed canonical source type excluded exactly."""

    source_type: SourceType


type RuleOperand = (
    ExactSourceIdOperand
    | FolderPrefixOperand
    | PathGlobOperand
    | ExtensionOperand
    | MediaTypeOperand
    | MaximumSizeOperand
    | SourceTypeOperand
)


def _expected_kind_for_operand(operand: RuleOperand) -> RuleKind:
    if isinstance(operand, ExactSourceIdOperand):
        return RuleKind.EXACT_SOURCE_ID
    if isinstance(operand, FolderPrefixOperand):
        return RuleKind.FOLDER_PREFIX
    if isinstance(operand, PathGlobOperand):
        return RuleKind.PATH_GLOB
    if isinstance(operand, ExtensionOperand):
        return RuleKind.EXTENSION
    if isinstance(operand, MediaTypeOperand):
        return RuleKind.MEDIA_TYPE
    if isinstance(operand, MaximumSizeOperand):
        return RuleKind.MAXIMUM_SIZE
    return RuleKind.SOURCE_TYPE


def _validate_fingerprint_hex(value: str) -> None:
    if len(value) != _FINGERPRINT_HEX_LENGTH or any(char not in _HEX_LOWER for char in value):
        raise ValueError("semantic fingerprint must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class ExclusionRule:
    """Immutable normalized rule: kind, exactly one typed operand, fingerprint.

    ``semantic_fingerprint`` is the lowercase SHA-256 over the rule contract
    tag, the rule kind and the normalized operand; duplicate fingerprints
    inside one revision are rejected at the revision invariant below.
    """

    rule_id: UUID
    rule_kind: RuleKind
    operand: RuleOperand
    semantic_fingerprint: str

    def __post_init__(self) -> None:
        reject_nil_uuid("rule_id", self.rule_id)
        if _expected_kind_for_operand(self.operand) is not self.rule_kind:
            raise ValueError("operand does not match rule kind")
        _validate_fingerprint_hex(self.semantic_fingerprint)


@dataclass(frozen=True, slots=True)
class PolicySubject:
    """Canonical evaluation subject with optional evidence fields (spec 7).

    ``workspace_id`` is always present and server-derived at public
    boundaries. Optional fields are genuinely absent rather than defaulted: a
    rule that requires a missing field yields indeterminate, never a fabricated
    value. ``normalized_locator`` must already be normalized.
    """

    workspace_id: UUID
    source_id: UUID | None = None
    normalized_locator: str | None = None
    source_type: SourceType | None = None
    media_type: CanonicalMediaType | None = None
    size_bytes: int | None = None

    def __post_init__(self) -> None:
        reject_nil_uuid("workspace_id", self.workspace_id)


@dataclass(frozen=True, slots=True)
class ExclusionPolicyRevision:
    """Immutable revision of zero through 256 normalized rules (spec 6.1).

    Construction itself enforces the closed revision invariants — non-nil
    identities, a positive revision number, the rule-count ceiling and
    duplicate-free rule IDs and semantic fingerprints — so evaluation can
    never run over an ambiguous rule set.
    """

    policy_revision_id: UUID
    workspace_id: UUID
    revision_number: int
    rules: tuple[ExclusionRule, ...] = ()

    def __post_init__(self) -> None:
        reject_nil_uuid("policy_revision_id", self.policy_revision_id)
        reject_nil_uuid("workspace_id", self.workspace_id)
        if self.revision_number < 1:
            raise ValueError("revision_number must be at least 1")
        if len(self.rules) > MAXIMUM_RULES_PER_REVISION:
            raise ValueError(f"revision must contain at most {MAXIMUM_RULES_PER_REVISION} rules")
        rule_ids = {rule.rule_id for rule in self.rules}
        if len(rule_ids) != len(self.rules):
            raise ValueError("duplicate rule_id in revision")
        fingerprints = {rule.semantic_fingerprint for rule in self.rules}
        if len(fingerprints) != len(self.rules):
            raise ValueError("duplicate semantic fingerprint in revision")
