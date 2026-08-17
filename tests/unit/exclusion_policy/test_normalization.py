"""Locator, operand and glob normalization proven against the spec grammar.

Covers NFC equivalence, separator and authority rejection, segment grammar,
UTF-8/segment bounds, every operand grammar (extension, media type, maximum
size, source type, folder prefix, glob), the closed glob token vocabulary,
wildcard bounds, deterministic semantic fingerprints and bounded property-style
sweeps over hostile locator and pattern shapes using only the stdlib.
"""

from __future__ import annotations

import random
from uuid import UUID, uuid4

import pytest

from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import RuleKind
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.normalization import (
    compile_glob,
    glob_matches,
    normalize_locator,
    normalize_rule,
)
from personal_os.sources.commands import SourceType

SUBJECT_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-0000000000a1")


def _reason_of(error: ExclusionPolicyError) -> str:
    reason = error.safe_details["reason"]
    return str(reason)


def test_normalize_locator_applies_nfc_and_is_idempotent() -> None:
    decomposed = "cafe\u0301/sub"
    assert normalize_locator(decomposed) == "caf\u00e9/sub"
    normalized = normalize_locator("caf\u00e9/sub/gra\u0301file")
    assert normalize_locator(normalized) == normalized
    assert normalize_locator("plain/Notes/a.md") == "plain/Notes/a.md"


def test_normalize_locator_rejects_separator_and_authority_shapes() -> None:
    cases = {
        "": "locator_empty",
        "/absolute/a.md": "locator_absolute",
        "notes/": "locator_trailing_separator",
        "//authority/a.md": "locator_absolute",
        "notes\\a.md": "locator_backslash_separator",
        "c:/notes/a.md": "locator_scheme_or_drive",
        "file:///notes/a.md": "locator_scheme_or_drive",
    }
    for value, reason in cases.items():
        with pytest.raises(ExclusionPolicyError) as error_info:
            normalize_locator(value)
        assert error_info.value.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
        assert _reason_of(error_info.value) == reason


def test_normalize_locator_rejects_invalid_and_control_segments() -> None:
    cases = {
        "notes//a.md": "locator_invalid_segment",
        "notes/./a.md": "locator_invalid_segment",
        "notes/../a.md": "locator_invalid_segment",
        ".": "locator_invalid_segment",
        "..": "locator_invalid_segment",
        "notes/\x00a.md": "locator_control_character",
        "notes/a\x1fb.md": "locator_control_character",
        "notes/a\nb.md": "locator_control_character",
    }
    for value, reason in cases.items():
        with pytest.raises(ExclusionPolicyError) as error_info:
            normalize_locator(value)
        assert _reason_of(error_info.value) == reason


def test_normalize_locator_enforces_utf8_and_segment_bounds() -> None:
    with pytest.raises(ExclusionPolicyError) as too_long:
        normalize_locator("a" * 4097)
    assert _reason_of(too_long.value) == "locator_too_long"
    with pytest.raises(ExclusionPolicyError) as too_many_segments:
        normalize_locator("/".join(f"s{i:02d}" for i in range(257)))
    assert _reason_of(too_many_segments.value) == "locator_too_many_segments"
    with pytest.raises(ExclusionPolicyError) as segment_too_long:
        normalize_locator("a" * 256)
    assert _reason_of(segment_too_long.value) == "locator_segment_too_long"


def test_normalize_locator_accepts_exact_bounds_and_keeps_literals() -> None:
    exact_byte_bound = "/".join(["a" * 255] * 15 + ["b" * 200, "c" * 55])
    assert len(exact_byte_bound.encode("utf-8")) == 4096
    assert normalize_locator(exact_byte_bound) == exact_byte_bound
    assert normalize_locator("/".join(f"s{i:02d}" for i in range(256))) == "/".join(
        f"s{i:02d}" for i in range(256)
    )
    assert normalize_locator("a" * 255) == "a" * 255
    literal = "100%25/caf\u00e9 note.md"
    assert normalize_locator(literal) == literal


def test_normalize_rule_extension_operand_grammar() -> None:
    assert normalize_rule(uuid4(), RuleKind.EXTENSION, text_operand=".pdf").operand == (
        normalize_rule(uuid4(), RuleKind.EXTENSION, text_operand=".pdf").operand
    )
    assert (
        normalize_rule(uuid4(), RuleKind.EXTENSION, text_operand=".PDF").operand.extension == ".pdf"
    )
    assert (
        normalize_rule(uuid4(), RuleKind.EXTENSION, text_operand=".tar.gz").operand.extension
        == ".tar.gz"
    )
    for invalid in ("pdf", ".", "p", ".p df", ".pdf!", ".café", ".a" * 33, "", ".PDF;"):
        with pytest.raises(ExclusionPolicyError) as error_info:
            normalize_rule(uuid4(), RuleKind.EXTENSION, text_operand=invalid, rule_index=3)
        assert error_info.value.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
        assert _reason_of(error_info.value) == "operand_invalid"
        assert error_info.value.safe_details["rule_index"] == 3


def test_normalize_rule_media_type_operand_grammar() -> None:
    exact = normalize_rule(uuid4(), RuleKind.MEDIA_TYPE, text_operand="application/pdf")
    assert exact.operand == exact.operand
    family = normalize_rule(uuid4(), RuleKind.MEDIA_TYPE, text_operand="text/*")
    assert family.operand.family_type == "text"
    for invalid in ("text/*; charset=utf-8", "*/*", "TEXT/*", "text", "text/plain/x", " /", ""):
        with pytest.raises(ExclusionPolicyError) as error_info:
            normalize_rule(uuid4(), RuleKind.MEDIA_TYPE, text_operand=invalid)
        assert _reason_of(error_info.value) == "operand_invalid"


def test_normalize_rule_maximum_size_operand_bounds() -> None:
    for valid in (0, 1, 104857600):
        normalized = normalize_rule(uuid4(), RuleKind.MAXIMUM_SIZE, size_bytes_operand=valid)
        assert normalized.operand.maximum_size_bytes == valid
    for invalid in (-1, 104857601, 2**63):
        with pytest.raises(ExclusionPolicyError) as error_info:
            normalize_rule(uuid4(), RuleKind.MAXIMUM_SIZE, size_bytes_operand=invalid)
        assert _reason_of(error_info.value) == "operand_invalid"
    with pytest.raises(ExclusionPolicyError):
        normalize_rule(uuid4(), RuleKind.MAXIMUM_SIZE, size_bytes_operand=True)  # type: ignore[arg-type]


def test_normalize_rule_source_type_operand_is_the_closed_enum() -> None:
    normalized = normalize_rule(uuid4(), RuleKind.SOURCE_TYPE, text_operand="markdown")
    assert normalized.operand.source_type is SourceType.MARKDOWN
    with pytest.raises(ExclusionPolicyError) as error_info:
        normalize_rule(uuid4(), RuleKind.SOURCE_TYPE, text_operand="carrier-pigeon")
    assert _reason_of(error_info.value) == "operand_invalid"


def test_normalize_rule_exact_source_id_operand() -> None:
    normalized = normalize_rule(
        uuid4(), RuleKind.EXACT_SOURCE_ID, source_id_operand=SUBJECT_SOURCE_ID
    )
    assert normalized.operand.source_id == SUBJECT_SOURCE_ID
    with pytest.raises(ExclusionPolicyError) as error_info:
        normalize_rule(uuid4(), RuleKind.EXACT_SOURCE_ID, source_id_operand=UUID(int=0))
    assert _reason_of(error_info.value) == "operand_invalid"


def test_normalize_rule_folder_prefix_uses_locator_grammar() -> None:
    normalized = normalize_rule(uuid4(), RuleKind.FOLDER_PREFIX, text_operand="private")
    assert normalized.operand.folder_prefix == "private"
    nfc = normalize_rule(uuid4(), RuleKind.FOLDER_PREFIX, text_operand="cafe\u0301")
    assert nfc.operand.folder_prefix == "caf\u00e9"
    for invalid in ("private/", "/private", "private//sub", "../private", ""):
        with pytest.raises(ExclusionPolicyError):
            normalize_rule(uuid4(), RuleKind.FOLDER_PREFIX, text_operand=invalid)


def test_normalize_rule_requires_exactly_one_operand() -> None:
    with pytest.raises(ExclusionPolicyError) as missing:
        normalize_rule(uuid4(), RuleKind.EXTENSION)
    assert _reason_of(missing.value) == "operand_missing"
    with pytest.raises(ExclusionPolicyError) as conflict:
        normalize_rule(
            uuid4(),
            RuleKind.EXTENSION,
            text_operand=".pdf",
            size_bytes_operand=10,
        )
    assert _reason_of(conflict.value) == "operand_conflict"
    with pytest.raises(ExclusionPolicyError) as nil_id:
        normalize_rule(UUID(int=0), RuleKind.EXTENSION, text_operand=".pdf")
    assert _reason_of(nil_id.value) == "rule_id_invalid"


def test_compile_glob_supports_the_closed_token_vocabulary() -> None:
    assert glob_matches(compile_glob("**/*.md"), ("notes", "a.md"))
    assert glob_matches(compile_glob("**/*.md"), ("a.md",))
    assert glob_matches(compile_glob("a/**/b"), ("a", "b"))
    assert glob_matches(compile_glob("a/**/b"), ("a", "x", "y", "b"))
    assert glob_matches(compile_glob("*.md"), ("a.md",))
    assert not glob_matches(compile_glob("*.md"), ("sub", "a.md"))
    assert glob_matches(compile_glob("a*b/c.md"), ("ab", "c.md"))
    assert glob_matches(compile_glob("a*b/c.md"), ("axxb", "c.md"))
    assert glob_matches(compile_glob("a**b/c.md"), ("axxb", "c.md"))
    assert not glob_matches(compile_glob("a**b/c.md"), ("a", "b"))
    assert glob_matches(compile_glob("**"), ("deep", "path", "file.md"))
    assert glob_matches(compile_glob("notes/**"), ("notes", "deep", "a.md"))
    assert glob_matches(compile_glob("notes/**"), ("notes",))
    assert compile_glob("*.md").wildcard_token_count == 1
    assert compile_glob("**/*.md").wildcard_token_count == 3


def test_compile_glob_rejects_unsupported_tokens_and_bounds() -> None:
    cases = {
        "a?b.md": "glob_unsupported_token",
        "no[oe].md": "glob_unsupported_token",
        "no{ne}.md": "glob_unsupported_token",
        "no]e.md": "glob_unsupported_token",
        "seg/!negation.md": "glob_unsupported_token",
        "notes\\a.md": "locator_backslash_separator",
        "/absolute.md": "locator_absolute",
        "notes/": "locator_trailing_separator",
        ".": "locator_invalid_segment",
    }
    for pattern, reason in cases.items():
        with pytest.raises(ExclusionPolicyError) as error_info:
            compile_glob(pattern)
        assert _reason_of(error_info.value) == reason
    with pytest.raises(ExclusionPolicyError) as too_long:
        compile_glob("a" * 1025)
    assert _reason_of(too_long.value) == "glob_too_long"
    with pytest.raises(ExclusionPolicyError) as too_many_segments:
        compile_glob("/".join(f"s{i:02d}" for i in range(65)))
    assert _reason_of(too_many_segments.value) == "glob_too_many_segments"
    with pytest.raises(ExclusionPolicyError) as too_many_wildcards:
        compile_glob("*".join("abcdefghijklmnopqr"))
    assert _reason_of(too_many_wildcards.value) == "glob_too_many_wildcards"


def test_compile_glob_accepts_exact_bounds() -> None:
    assert len(compile_glob("a" * 1024).segments) == 1
    compiled = compile_glob("/".join(f"s{i:02d}" for i in range(64)))
    assert len(compiled.segments) == 64
    assert compile_glob("*".join("abcdefghijklmnopq")).wildcard_token_count == 16


def test_semantic_fingerprint_is_deterministic_and_operand_sensitive() -> None:
    first = normalize_rule(uuid4(), RuleKind.EXTENSION, text_operand=".pdf")
    second = normalize_rule(uuid4(), RuleKind.EXTENSION, text_operand=".pdf")
    other = normalize_rule(uuid4(), RuleKind.EXTENSION, text_operand=".md")
    assert first.semantic_fingerprint == second.semantic_fingerprint
    assert first.semantic_fingerprint != other.semantic_fingerprint
    nfc_equivalent = normalize_rule(uuid4(), RuleKind.FOLDER_PREFIX, text_operand="cafe\u0301")
    precomposed = normalize_rule(uuid4(), RuleKind.FOLDER_PREFIX, text_operand="caf\u00e9")
    assert nfc_equivalent.semantic_fingerprint == precomposed.semantic_fingerprint
    for rule in (first, other, nfc_equivalent):
        assert len(rule.semantic_fingerprint) == 64
        assert rule.semantic_fingerprint == rule.semantic_fingerprint.lower()


def test_hostile_locators_are_rejected_or_renormalize_identically() -> None:
    rng = random.Random(20260817)
    hostile_alphabet = "abc/*?\\[]{}!.\x00\x1f:/ caf\u00e9\u0301\n\t"
    benign_alphabet = "abc/._-caf\u00e9note "
    accepted = 0
    for index in range(400):
        if index % 2:
            alphabet = hostile_alphabet
            length = rng.randint(0, 60)
        else:
            alphabet = benign_alphabet
            length = rng.randint(1, 120)
        candidate = "".join(rng.choice(alphabet) for _ in range(length))
        try:
            normalized = normalize_locator(candidate)
        except ExclusionPolicyError:
            continue
        accepted += 1
        assert normalize_locator(normalized) == normalized
        assert "\\" not in normalized
        assert "\x00" not in normalized
        assert len(normalized.encode("utf-8")) <= 4096
        assert len(normalized.split("/")) <= 256
    assert accepted > 100


def test_hostile_glob_patterns_complete_bounded_matching() -> None:
    rng = random.Random(20260818)
    hostile_alphabet = "ab/*?\\[]{}!."
    benign_alphabet = "abc*/."
    sample_paths = (
        ("a",),
        ("a", "b"),
        ("x", "y", "z.md"),
        ("a-b-c", "d_e.f"),
    )
    compiled_count = 0
    for index in range(400):
        if index % 2:
            alphabet = hostile_alphabet
            length = rng.randint(0, 40)
        else:
            alphabet = benign_alphabet
            length = rng.randint(1, 30)
        pattern = "".join(rng.choice(alphabet) for _ in range(length))
        try:
            compiled = compile_glob(pattern)
        except ExclusionPolicyError:
            continue
        compiled_count += 1
        for segments in sample_paths:
            assert isinstance(glob_matches(compiled, segments), bool)
    assert compiled_count > 50


def test_pathological_glob_matching_stays_bounded() -> None:
    pattern = compile_glob("a" + "*a" * 8)
    value = ("a" * 255,)
    assert glob_matches(pattern, value) is True
    star_heavy = compile_glob("*".join("x" * 16))
    assert isinstance(glob_matches(star_heavy, ("y" * 255,)), bool)
