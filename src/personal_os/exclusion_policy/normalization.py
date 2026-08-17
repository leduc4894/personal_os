"""Bounded locator, operand and glob normalization shared by backend and plugin.

``normalize_locator`` implements the spec 6.3 grammar: NFC Unicode, ``/`` as
the only separator with backslash rejected rather than rewritten, relative
paths without scheme, drive letter or authority, no empty, ``.`` or ``..``
segments, no NUL or control characters, and the 4,096-byte/256-segment/
255-byte-per-segment ceilings. Percent signs and Unicode characters stay
literal; there is no URL decode, locale collation or platform-dependent case
folding.

``compile_glob`` implements the closed spec 6.4 grammar over the same
normalized-path rules: ``*`` is the only in-segment wildcard, ``**`` is
special only as a complete segment, regex syntax, ``?``, character classes,
braces, negation and escapes are rejected, and operands are bounded to 1,024
UTF-8 bytes, 64 segments and 16 wildcard tokens. Matching runs over the
compiled token structure (a segment-level reachability walk plus a greedy
in-segment scan); untrusted patterns never become a regular expression.

``normalize_rule`` is the only sanctioned rule constructor: it validates the
closed kind-to-operand mapping, applies the operand grammar above and computes
the lowercase SHA-256 semantic fingerprint over the rule contract tag, the
rule kind and the normalized operand. Every rejection raises the typed
``exclusion_policy_input_invalid`` error with a closed reason token and,
where known, the zero-based ``rule_index``.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Final
from uuid import UUID

from personal_os.exclusion_policy.contracts import (
    EXTENSION_MAXIMUM_CHARACTERS,
    EXTENSION_MINIMUM_CHARACTERS,
    GLOB_MAXIMUM_BYTES,
    GLOB_MAXIMUM_SEGMENTS,
    GLOB_MAXIMUM_WILDCARD_TOKENS,
    LOCATOR_MAXIMUM_BYTES,
    LOCATOR_MAXIMUM_SEGMENTS,
    LOCATOR_SEGMENT_MAXIMUM_BYTES,
    MAXIMUM_SIZE_BYTES_CEILING,
    CompiledGlob,
    ExactSourceIdOperand,
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
    RuleKind,
    RuleOperand,
    SourceTypeOperand,
)
from personal_os.exclusion_policy.errors import (
    GLOB_TOO_LONG,
    GLOB_TOO_MANY_SEGMENTS,
    GLOB_TOO_MANY_WILDCARDS,
    GLOB_UNSUPPORTED_TOKEN,
    LOCATOR_ABSOLUTE,
    LOCATOR_BACKSLASH_SEPARATOR,
    LOCATOR_CONTROL_CHARACTER,
    LOCATOR_EMPTY,
    LOCATOR_INVALID_SEGMENT,
    LOCATOR_NOT_VALID_UNICODE,
    LOCATOR_SCHEME_OR_DRIVE,
    LOCATOR_SEGMENT_TOO_LONG,
    LOCATOR_TOO_LONG,
    LOCATOR_TOO_MANY_SEGMENTS,
    LOCATOR_TRAILING_SEPARATOR,
    OPERAND_CONFLICT,
    OPERAND_INVALID,
    OPERAND_MISSING,
    RULE_ID_INVALID,
    input_invalid,
)
from personal_os.object_storage import CanonicalMediaType
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import SourceType

#: Contract tag hashed into every rule semantic fingerprint.
RULE_FINGERPRINT_CONTRACT: Final[str] = "exclusion_policy_rule/v1"

_EXTENSION_CHARACTERS: Final[frozenset[str]] = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
#: RFC 2045 ``tspecials`` plus ``/`` and ``*``; mirrors the canonical MIME
#: token grammar in ``personal_os.object_storage.keys`` for family type parts.
_MIME_TSPECIALS: Final[frozenset[str]] = frozenset(
    {"(", ")", "<", ">", "@", ",", ";", ":", "\\", '"', "[", "]", "?", "=", "/", "*"}
)
_GLOB_FORBIDDEN_CHARACTERS: Final[frozenset[str]] = frozenset("?[]{}")


def _nfc_or_reject(value: str, rule_index: int | None) -> str:
    """Normalize to NFC, rejecting text that is not valid Unicode."""

    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        raise input_invalid(LOCATOR_NOT_VALID_UNICODE, rule_index) from None
    return normalized


def _reject_control_characters(normalized: str, rule_index: int | None) -> None:
    for char in normalized:
        if unicodedata.category(char) == "Cc":
            raise input_invalid(LOCATOR_CONTROL_CHARACTER, rule_index)


def _reject_scheme_or_drive(segments: list[str], rule_index: int | None) -> None:
    if segments and ":" in segments[0]:
        raise input_invalid(LOCATOR_SCHEME_OR_DRIVE, rule_index)


def _reject_invalid_segments(segments: list[str], rule_index: int | None) -> None:
    for segment in segments:
        if segment in ("", ".", ".."):
            raise input_invalid(LOCATOR_INVALID_SEGMENT, rule_index)


def _utf8_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def normalize_locator(value: str) -> str:
    """Normalize one Vault locator to the canonical NFC relative form.

    The result uses ``/`` separators, no leading or trailing separator, no
    scheme, drive letter or authority, only non-special segments and stays
    within the locator byte and segment ceilings. Percent signs and Unicode
    characters are literal; the value is not case-folded.
    """

    if not isinstance(value, str):
        raise input_invalid(LOCATOR_NOT_VALID_UNICODE)
    normalized = _nfc_or_reject(value, None)
    if not normalized:
        raise input_invalid(LOCATOR_EMPTY)
    if "\\" in normalized:
        raise input_invalid(LOCATOR_BACKSLASH_SEPARATOR)
    if normalized.startswith("/"):
        raise input_invalid(LOCATOR_ABSOLUTE)
    if normalized.endswith("/"):
        raise input_invalid(LOCATOR_TRAILING_SEPARATOR)
    _reject_control_characters(normalized, None)
    segments = normalized.split("/")
    _reject_scheme_or_drive(segments, None)
    _reject_invalid_segments(segments, None)
    if len(segments) > LOCATOR_MAXIMUM_SEGMENTS:
        raise input_invalid(LOCATOR_TOO_MANY_SEGMENTS)
    if _utf8_bytes(normalized) > LOCATOR_MAXIMUM_BYTES:
        raise input_invalid(LOCATOR_TOO_LONG)
    for segment in segments:
        if _utf8_bytes(segment) > LOCATOR_SEGMENT_MAXIMUM_BYTES:
            raise input_invalid(LOCATOR_SEGMENT_TOO_LONG)
    return normalized


def _normalize_glob_text(pattern: str, rule_index: int | None) -> str:
    """Validate one glob operand and return its NFC text."""

    if not isinstance(pattern, str):
        raise input_invalid(LOCATOR_NOT_VALID_UNICODE, rule_index)
    normalized = _nfc_or_reject(pattern, rule_index)
    if not normalized:
        raise input_invalid(LOCATOR_EMPTY, rule_index)
    if any(char in _GLOB_FORBIDDEN_CHARACTERS for char in normalized):
        raise input_invalid(GLOB_UNSUPPORTED_TOKEN, rule_index)
    if "\\" in normalized:
        raise input_invalid(LOCATOR_BACKSLASH_SEPARATOR, rule_index)
    if normalized.startswith("/"):
        raise input_invalid(LOCATOR_ABSOLUTE, rule_index)
    if normalized.endswith("/"):
        raise input_invalid(LOCATOR_TRAILING_SEPARATOR, rule_index)
    segments = normalized.split("/")
    if any(segment.startswith("!") for segment in segments):
        raise input_invalid(GLOB_UNSUPPORTED_TOKEN, rule_index)
    _reject_control_characters(normalized, rule_index)
    _reject_scheme_or_drive(segments, rule_index)
    _reject_invalid_segments(segments, rule_index)
    if len(segments) > GLOB_MAXIMUM_SEGMENTS:
        raise input_invalid(GLOB_TOO_MANY_SEGMENTS, rule_index)
    if _utf8_bytes(normalized) > GLOB_MAXIMUM_BYTES:
        raise input_invalid(GLOB_TOO_LONG, rule_index)
    if normalized.count("*") > GLOB_MAXIMUM_WILDCARD_TOKENS:
        raise input_invalid(GLOB_TOO_MANY_WILDCARDS, rule_index)
    return normalized


def _compile_segment(segment: str) -> GlobSegment:
    if segment == "**":
        return GlobSegment(is_double_star=True, parts=())
    parts: list[GlobSegmentPart] = []
    literal: list[str] = []
    for char in segment:
        if char == "*":
            if literal:
                parts.append(GlobLiteralPart(text="".join(literal)))
                literal.clear()
            parts.append(GlobStarPart())
        else:
            literal.append(char)
    if literal:
        parts.append(GlobLiteralPart(text="".join(literal)))
    return GlobSegment(is_double_star=False, parts=tuple(parts))


def _compile_normalized_glob(normalized: str) -> CompiledGlob:
    segments = tuple(_compile_segment(segment) for segment in normalized.split("/"))
    return CompiledGlob(
        segments=segments,
        wildcard_token_count=normalized.count("*"),
    )


def compile_glob(pattern: str) -> CompiledGlob:
    """Normalize and compile one glob operand into its bounded token form."""

    return _compile_normalized_glob(_normalize_glob_text(pattern, None))


def _segment_matches(parts: tuple[GlobSegmentPart, ...], value: str) -> bool:
    """Match one segment against literal/star parts with a bounded greedy scan.

    Classic wildcard loop: a mismatch backtracks only to the most recent
    ``*``, so the scan is bounded by the segment length times the wildcard
    count rather than exploring exponential alternatives.
    """

    part_count = len(parts)
    value_length = len(value)
    part_index = 0
    value_index = 0
    backtrack_part = -1
    backtrack_value = 0
    while value_index < value_length:
        if part_index < part_count:
            part = parts[part_index]
            if isinstance(part, GlobLiteralPart):
                if value.startswith(part.text, value_index):
                    value_index += len(part.text)
                    part_index += 1
                    continue
            else:
                backtrack_part = part_index
                backtrack_value = value_index
                part_index += 1
                continue
        if backtrack_part >= 0:
            backtrack_value += 1
            value_index = backtrack_value
            part_index = backtrack_part + 1
            continue
        return False
    while part_index < part_count:
        if not isinstance(parts[part_index], GlobStarPart):
            return False
        part_index += 1
    return True


def glob_matches(compiled: CompiledGlob, locator_segments: tuple[str, ...]) -> bool:
    """Match pre-split normalized locator segments against a compiled glob.

    The whole locator must match: single-star segments match exactly one path
    segment, and a complete ``**`` segment matches zero or more whole
    segments. The reachability walk is bounded by the glob and locator
    segment ceilings.
    """

    path_count = len(locator_segments)
    reachable = [False] * (path_count + 1)
    reachable[0] = True
    for segment in compiled.segments:
        next_reachable = [False] * (path_count + 1)
        if segment.is_double_star:
            prefix_reachable = False
            for index in range(path_count + 1):
                prefix_reachable = prefix_reachable or reachable[index]
                next_reachable[index] = prefix_reachable
        else:
            for index in range(path_count):
                if reachable[index] and _segment_matches(segment.parts, locator_segments[index]):
                    next_reachable[index + 1] = True
        reachable = next_reachable
    return reachable[path_count]


def fold_ascii_lowercase(value: str) -> str:
    """Fold only ASCII ``A``-``Z``; other code points stay literal (spec 6.3)."""

    return "".join(chr(ord(char) + 32) if "A" <= char <= "Z" else char for char in value)


def _is_family_type_token(value: str) -> bool:
    if not value:
        return False
    for char in value:
        code_point = ord(char)
        if code_point < 33 or code_point > 126:
            return False
        if char in _MIME_TSPECIALS:
            return False
        if "A" <= char <= "Z":
            return False
    return True


def _normalize_extension(text_operand: str, rule_index: int | None) -> str:
    folded = fold_ascii_lowercase(text_operand)
    length = len(folded)
    if length < EXTENSION_MINIMUM_CHARACTERS or length > EXTENSION_MAXIMUM_CHARACTERS:
        raise input_invalid(OPERAND_INVALID, rule_index)
    if not folded.startswith("."):
        raise input_invalid(OPERAND_INVALID, rule_index)
    for char in folded:
        if char not in _EXTENSION_CHARACTERS:
            raise input_invalid(OPERAND_INVALID, rule_index)
    return folded


def _normalize_media_type_operand(text_operand: str, rule_index: int | None) -> MediaTypeOperand:
    type_part, separator, subtype_part = text_operand.partition("/")
    if separator == "/" and subtype_part == "*":
        if not _is_family_type_token(type_part):
            raise input_invalid(OPERAND_INVALID, rule_index)
        return MediaTypeOperand(exact_media_type=None, family_type=type_part)
    try:
        exact = CanonicalMediaType.parse(text_operand)
    except ValueError:
        raise input_invalid(OPERAND_INVALID, rule_index) from None
    return MediaTypeOperand(exact_media_type=exact, family_type=None)


def _fingerprint_operand_value(operand: RuleOperand) -> tuple[str, object]:
    """Return the named typed operand field and its canonical fingerprint value."""

    if isinstance(operand, ExactSourceIdOperand):
        return "source_id", str(operand.source_id)
    if isinstance(operand, FolderPrefixOperand):
        return "folder_prefix", operand.folder_prefix
    if isinstance(operand, PathGlobOperand):
        return "path_glob", operand.normalized_pattern
    if isinstance(operand, ExtensionOperand):
        return "extension", operand.extension
    if isinstance(operand, MediaTypeOperand):
        if operand.exact_media_type is not None:
            return "media_type", operand.exact_media_type.value
        return "media_type", f"{operand.family_type}/*"
    if isinstance(operand, MaximumSizeOperand):
        return "maximum_size_bytes", operand.maximum_size_bytes
    return "source_type", operand.source_type.value


def _compute_semantic_fingerprint(rule_kind: RuleKind, operand: RuleOperand) -> str:
    operand_field, operand_value = _fingerprint_operand_value(operand)
    envelope: dict[str, object] = {
        "contract": RULE_FINGERPRINT_CONTRACT,
        "rule_kind": rule_kind.value,
        operand_field: operand_value,
    }
    canonical_bytes = json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def normalize_rule(
    rule_id: UUID,
    rule_kind: RuleKind,
    *,
    source_id_operand: UUID | None = None,
    text_operand: str | None = None,
    size_bytes_operand: int | None = None,
    rule_index: int | None = None,
) -> ExclusionRule:
    """Validate and normalize one rule into its immutable domain value.

    Exactly one typed operand column may be populated for the closed rule
    kind; folder-prefix, glob and extension operands are normalized with the
    shared locator/glob grammar, media-type operands follow the canonical MIME
    grammar plus the ``type/*`` family form, maximum-size operands are
    inclusive in ``0..104857600`` and source-type operands must name a closed
    ``SourceType`` member.
    """

    try:
        reject_nil_uuid("rule_id", rule_id)
    except ValueError:
        raise input_invalid(RULE_ID_INVALID, rule_index) from None

    populated = [
        operand
        for operand in (source_id_operand, text_operand, size_bytes_operand)
        if operand is not None
    ]
    if not populated:
        raise input_invalid(OPERAND_MISSING, rule_index)
    if len(populated) > 1:
        raise input_invalid(OPERAND_CONFLICT, rule_index)

    operand: RuleOperand
    if rule_kind is RuleKind.EXACT_SOURCE_ID:
        if not isinstance(source_id_operand, UUID):
            raise input_invalid(OPERAND_INVALID, rule_index)
        try:
            reject_nil_uuid("source_id_operand", source_id_operand)
        except ValueError:
            raise input_invalid(OPERAND_INVALID, rule_index) from None
        operand = ExactSourceIdOperand(source_id=source_id_operand)
    elif rule_kind is RuleKind.FOLDER_PREFIX:
        if not isinstance(text_operand, str):
            raise input_invalid(OPERAND_INVALID, rule_index)
        operand = FolderPrefixOperand(folder_prefix=normalize_locator(text_operand))
    elif rule_kind is RuleKind.PATH_GLOB:
        if not isinstance(text_operand, str):
            raise input_invalid(OPERAND_INVALID, rule_index)
        normalized_pattern = _normalize_glob_text(text_operand, rule_index)
        operand = PathGlobOperand(
            normalized_pattern=normalized_pattern,
            compiled=_compile_normalized_glob(normalized_pattern),
        )
    elif rule_kind is RuleKind.EXTENSION:
        if not isinstance(text_operand, str):
            raise input_invalid(OPERAND_INVALID, rule_index)
        operand = ExtensionOperand(extension=_normalize_extension(text_operand, rule_index))
    elif rule_kind is RuleKind.MEDIA_TYPE:
        if not isinstance(text_operand, str):
            raise input_invalid(OPERAND_INVALID, rule_index)
        operand = _normalize_media_type_operand(text_operand, rule_index)
    elif rule_kind is RuleKind.MAXIMUM_SIZE:
        if isinstance(size_bytes_operand, bool) or not isinstance(size_bytes_operand, int):
            raise input_invalid(OPERAND_INVALID, rule_index)
        if size_bytes_operand < 0 or size_bytes_operand > MAXIMUM_SIZE_BYTES_CEILING:
            raise input_invalid(OPERAND_INVALID, rule_index)
        operand = MaximumSizeOperand(maximum_size_bytes=size_bytes_operand)
    else:
        if not isinstance(text_operand, str):
            raise input_invalid(OPERAND_INVALID, rule_index)
        try:
            source_type = SourceType(text_operand)
        except ValueError:
            raise input_invalid(OPERAND_INVALID, rule_index) from None
        operand = SourceTypeOperand(source_type=source_type)

    return ExclusionRule(
        rule_id=rule_id,
        rule_kind=rule_kind,
        operand=operand,
        semantic_fingerprint=_compute_semantic_fingerprint(rule_kind, operand),
    )
