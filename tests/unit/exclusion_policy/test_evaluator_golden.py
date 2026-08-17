"""Shared evaluator golden corpus: fixture replay, determinism and safety.

The fixture is the Python/TypeScript shared golden corpus for locator
normalization, rule normalization and deny-only evaluation. It contains only
synthetic test values (fixed UUIDs and synthetic locators/operands), is stored
as canonical deterministic JSON (sorted keys, two-space indent, UTF-8, single
trailing newline) and pins NFC equivalence, slash rules, case rules, folder
boundaries, every supported glob token, the empty policy, missing-field
indeterminacy, invalid operands, maximum-size equality and multi-rule
precedence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

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
from personal_os.exclusion_policy.normalization import (
    normalize_locator,
    normalize_rule,
)
from personal_os.object_storage import CanonicalMediaType
from personal_os.sources.commands import SourceType

FIXTURE_PATH = Path(__file__).parents[2] / "fixtures" / "exclusion_policy" / "evaluator-golden.json"


def _load_fixture() -> dict[str, object]:
    with FIXTURE_PATH.open("r", encoding="utf-8") as fixture_file:
        return cast(dict[str, object], json.load(fixture_file))


def test_golden_fixture_bytes_are_canonical_and_deterministic() -> None:
    raw_bytes = FIXTURE_PATH.read_bytes()
    parsed = json.loads(raw_bytes)
    canonical = (
        json.dumps(parsed, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
    )
    assert canonical == raw_bytes
    assert parsed["contract"] == "exclusion_policy_evaluator_golden/v1"


def test_golden_normalization_cases_replay() -> None:
    fixture = _load_fixture()
    cases = cast(Sequence[Mapping[str, object]], fixture["normalization_cases"])
    assert len(cases) >= 8
    for case in cases:
        value = cast(str, case["value"])
        if "expected" in case:
            assert normalize_locator(value) == cast(str, case["expected"]), case["case_id"]
        else:
            with pytest.raises(ExclusionPolicyError) as error_info:
                normalize_locator(value)
            assert str(error_info.value.safe_details["reason"]) == case["error_reason"], case[
                "case_id"
            ]


def test_golden_rule_cases_replay_fingerprints() -> None:
    fixture = _load_fixture()
    cases = cast(Sequence[Mapping[str, object]], fixture["rule_cases"])
    assert len(cases) >= 7
    for case in cases:
        if "error_reason" in case:
            with pytest.raises(ExclusionPolicyError) as error_info:
                normalize_rule(
                    UUID(cast(str, case["rule_id"])),
                    RuleKind(cast(str, case["rule_kind"])),
                    source_id_operand=(
                        None
                        if case.get("source_id_operand") is None
                        else UUID(cast(str, case["source_id_operand"]))
                    ),
                    text_operand=cast("str | None", case.get("text_operand")),
                    size_bytes_operand=cast("int | None", case.get("size_bytes_operand")),
                    rule_index=cast(int, case["rule_index"]),
                )
            assert str(error_info.value.safe_details["reason"]) == case["error_reason"], case[
                "case_id"
            ]
            continue
        normalized = normalize_rule(
            UUID(cast(str, case["rule_id"])),
            RuleKind(cast(str, case["rule_kind"])),
            source_id_operand=(
                None
                if case.get("source_id_operand") is None
                else UUID(cast(str, case["source_id_operand"]))
            ),
            text_operand=cast("str | None", case.get("text_operand")),
            size_bytes_operand=cast("int | None", case.get("size_bytes_operand")),
        )
        assert normalized.semantic_fingerprint == case["expected_fingerprint"], case["case_id"]


def test_golden_evaluation_cases_replay_decisions() -> None:
    fixture = _load_fixture()
    workspace_id = UUID(cast(str, fixture["workspace_id"]))
    cases = cast(Sequence[Mapping[str, object]], fixture["evaluation_cases"])
    assert len(cases) >= 15
    for case in cases:
        rules = tuple(
            normalize_rule(
                UUID(cast(str, rule_case["rule_id"])),
                RuleKind(cast(str, rule_case["rule_kind"])),
                source_id_operand=(
                    None
                    if rule_case.get("source_id_operand") is None
                    else UUID(cast(str, rule_case["source_id_operand"]))
                ),
                text_operand=cast("str | None", rule_case.get("text_operand")),
                size_bytes_operand=cast("int | None", rule_case.get("size_bytes_operand")),
            )
            for rule_case in cast(Sequence[Mapping[str, object]], case["rules"])
        )
        revision = ExclusionPolicyRevision(
            policy_revision_id=UUID(cast(str, fixture["policy_revision_id"])),
            workspace_id=workspace_id,
            revision_number=cast(int, fixture["revision_number"]),
            rules=rules,
        )
        subject_case = cast(Mapping[str, object], case["subject"])
        subject = PolicySubject(
            workspace_id=workspace_id,
            source_id=(
                None
                if subject_case.get("source_id") is None
                else UUID(cast(str, subject_case["source_id"]))
            ),
            normalized_locator=cast("str | None", subject_case.get("normalized_locator")),
            source_type=(
                None
                if subject_case.get("source_type") is None
                else SourceType(cast(str, subject_case["source_type"]))
            ),
            media_type=(
                None
                if subject_case.get("media_type") is None
                else CanonicalMediaType.parse(cast(str, subject_case["media_type"]))
            ),
            size_bytes=cast("int | None", subject_case.get("size_bytes")),
        )
        decision = evaluate_policy(revision=revision, subject=subject)
        expected = cast(Mapping[str, object], case["expected"])
        assert decision.raw is RawPolicyDecision(cast(str, expected["raw"])), case["case_id"]
        assert decision.enforced is EnforcedPolicyDecision(cast(str, expected["enforced"])), case[
            "case_id"
        ]
        matched_ids = cast(Sequence[str], expected["matched_rule_ids"])
        assert decision.matched_rule_ids == tuple(
            UUID(cast(str, rule_id)) for rule_id in matched_ids
        ), case["case_id"]
        assert decision.missing_fields == tuple(
            PolicySubjectField(field) for field in cast(Sequence[str], expected["missing_fields"])
        ), case["case_id"]


def test_golden_fixture_carries_only_synthetic_case_values() -> None:
    fixture_text = FIXTURE_PATH.read_text(encoding="utf-8")
    parsed = json.loads(fixture_text)
    for forbidden_key in ("title", "content", "payload_sha256", "signature"):
        assert forbidden_key not in fixture_text
    assert isinstance(parsed["workspace_id"], str)
    assert str(UUID(cast(str, parsed["workspace_id"]))) == parsed["workspace_id"]
