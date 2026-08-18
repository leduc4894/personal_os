"""Strict exclusion-policy wire models and their domain boundary conversion.

These tests pin the request/response model contracts of the Task 8 API
surface: every model is frozen and closed for extra fields, the draft rule
grammar mirrors the signed-snapshot member encoding (one ``rule_id``, one
``rule_kind`` and exactly one named operand), conversion to domain values
happens only through the shared normalization gate with typed closed-reason
errors, and the response renderers emit exactly one operand member per rule
without ever carrying a subject fingerprint, a locator or a database object.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

import pytest
from api_runtime.exclusion_policy_models import (
    PolicyDraftReplaceRequest,
    PolicyDraftRuleRequest,
    PolicyPublicationData,
    PolicyPublicationRequest,
    policy_draft_data,
    to_domain_rule,
)
from pydantic import ValidationError

from personal_os.exclusion_policy.contracts import RuleKind
from personal_os.exclusion_policy.errors import (
    OPERAND_CONFLICT,
    OPERAND_INVALID,
    OPERAND_MISSING,
)
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.ports import PolicyDraft

RULE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-0000000000a1")
SOURCE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-0000000000b2")
DRAFT_ID: Final[UUID] = UUID("00000000-0000-7000-8000-0000000000c3")
WORKSPACE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-0000000000d4")


def test_rule_request_model_rejects_unknown_members() -> None:
    with pytest.raises(ValidationError):
        PolicyDraftRuleRequest.model_validate(
            {"rule_id": str(RULE_ID), "rule_kind": "extension", "extension": ".md", "note": "x"}
        )


def test_rule_request_model_rejects_workspace_selectors() -> None:
    with pytest.raises(ValidationError):
        PolicyDraftRuleRequest.model_validate(
            {
                "rule_id": str(RULE_ID),
                "rule_kind": "extension",
                "extension": ".md",
                "workspace_id": str(WORKSPACE_ID),
            }
        )


def test_draft_replace_request_is_strict_and_closed() -> None:
    with pytest.raises(ValidationError):
        PolicyDraftReplaceRequest.model_validate({"expected_draft_version": 1})
    with pytest.raises(ValidationError):
        PolicyDraftReplaceRequest.model_validate(
            {
                "expected_draft_version": 1,
                "rules": [],
                "workspace_id": str(WORKSPACE_ID),
            }
        )
    parsed = PolicyDraftReplaceRequest.model_validate({"expected_draft_version": 3, "rules": ()})
    assert parsed.expected_draft_version == 3
    assert parsed.rules == ()


def test_publication_request_is_strict_and_carries_the_exact_binding() -> None:
    body = {
        "policy_preview_id": "00000000-0000-7000-8000-0000000000e5",
        "policy_draft_id": str(DRAFT_ID),
        "expected_draft_version": 2,
        "expected_draft_sha256": "a" * 64,
        "preview_impact_digest": "b" * 64,
        "expected_active_policy_revision_id": None,
        "expected_active_revision_number": 0,
        "confirmation": "PUBLISH EXCLUSION POLICY",
    }
    parsed = PolicyPublicationRequest.model_validate(body)
    assert parsed.confirmation == "PUBLISH EXCLUSION POLICY"
    with pytest.raises(ValidationError):
        PolicyPublicationRequest.model_validate({**body, "signature": "client-supplied"})
    with pytest.raises(ValidationError):
        PolicyPublicationRequest.model_validate({**body, "revision_number": 7})
    with pytest.raises(ValidationError):
        PolicyPublicationRequest.model_validate({**body, "workspace_id": str(WORKSPACE_ID)})


@pytest.mark.parametrize(
    ("member", "value"),
    [
        ("source_id", str(SOURCE_ID)),
        ("folder_prefix", "notes/sub"),
        ("path_glob", "notes/*/private"),
        ("extension", ".md"),
        ("media_type", "text/markdown"),
        ("source_type", "markdown"),
    ],
)
def test_each_rule_kind_converts_through_the_normalization_gate(member: str, value: str) -> None:
    request = PolicyDraftRuleRequest.model_validate(
        {"rule_id": str(RULE_ID), "rule_kind": _kind_of(member), member: value}
    )
    rule = to_domain_rule(request, 0)
    assert rule.rule_id == RULE_ID
    assert rule.semantic_fingerprint  # normalization produced the digest


def test_maximum_size_rule_converts_to_the_size_operand() -> None:
    request = PolicyDraftRuleRequest.model_validate(
        {
            "rule_id": str(RULE_ID),
            "rule_kind": "maximum_size",
            "maximum_size_bytes": 1048576,
        }
    )
    rule = to_domain_rule(request, 4)
    assert rule.operand.maximum_size_bytes == 1048576


def test_missing_operand_for_the_kind_is_the_typed_missing_error() -> None:
    request = PolicyDraftRuleRequest.model_validate(
        {"rule_id": str(RULE_ID), "rule_kind": "extension"}
    )
    with pytest.raises(Exception) as raised:
        to_domain_rule(request, 2)
    error = raised.value
    assert error.error_code.value == "exclusion_policy_input_invalid"
    assert error.safe_details == {"reason": OPERAND_MISSING, "rule_index": 2}


def test_operand_of_another_kind_is_the_typed_conflict_error() -> None:
    request = PolicyDraftRuleRequest.model_validate(
        {"rule_id": str(RULE_ID), "rule_kind": "extension", "folder_prefix": "notes"}
    )
    with pytest.raises(Exception) as raised:
        to_domain_rule(request, 0)
    error = raised.value
    assert error.error_code.value == "exclusion_policy_input_invalid"
    assert error.safe_details == {"reason": OPERAND_CONFLICT, "rule_index": 0}


def test_two_operands_are_the_typed_conflict_error() -> None:
    request = PolicyDraftRuleRequest.model_validate(
        {
            "rule_id": str(RULE_ID),
            "rule_kind": "extension",
            "extension": ".md",
            "folder_prefix": "notes",
        }
    )
    with pytest.raises(Exception) as raised:
        to_domain_rule(request, 1)
    assert raised.value.safe_details["reason"] == OPERAND_CONFLICT


def test_invalid_operand_value_maps_to_the_typed_invalid_reason() -> None:
    request = PolicyDraftRuleRequest.model_validate(
        {"rule_id": str(RULE_ID), "rule_kind": "extension", "extension": "md"}
    )
    with pytest.raises(Exception) as raised:
        to_domain_rule(request, 0)
    assert raised.value.safe_details["reason"] == OPERAND_INVALID


def test_rule_id_nil_is_the_typed_invalid_reason() -> None:
    request = PolicyDraftRuleRequest.model_validate(
        {
            "rule_id": "00000000-0000-0000-0000-000000000000",
            "rule_kind": "extension",
            "extension": ".md",
        }
    )
    with pytest.raises(Exception) as raised:
        to_domain_rule(request, 0)
    assert raised.value.safe_details["reason"] is not None


def test_draft_renderer_emits_exactly_one_operand_member_per_rule() -> None:
    rules = (
        normalize_rule(RULE_ID, RuleKind.FOLDER_PREFIX, text_operand="notes/sub"),
        normalize_rule(
            UUID("00000000-0000-7000-8000-0000000000f6"),
            RuleKind.MAXIMUM_SIZE,
            size_bytes_operand=1024,
        ),
    )
    draft = PolicyDraft(
        draft_id=DRAFT_ID,
        workspace_id=WORKSPACE_ID,
        draft_version=5,
        base_policy_revision_id=None,
        rules=rules,
    )
    rendered = policy_draft_data(draft)
    assert rendered.draft_version == 5
    first = rendered.rules[0].model_dump(mode="json", exclude_unset=True)
    assert set(first) == {
        "rule_id",
        "rule_kind",
        "semantic_fingerprint",
        "folder_prefix",
    }
    second = rendered.rules[1].model_dump(mode="json", exclude_unset=True)
    assert set(second) == {
        "rule_id",
        "rule_kind",
        "semantic_fingerprint",
        "maximum_size_bytes",
    }


def test_publication_data_never_carries_signature_or_payload_bytes() -> None:
    data = PolicyPublicationData(
        workspace_id=WORKSPACE_ID,
        policy_revision_id=UUID("00000000-0000-7000-8000-000000000011"),
        revision_number=1,
        parent_policy_revision_id=None,
        payload_sha256="c" * 64,
        signing_key_id="ed25519-sha256-" + "A" * 43,
        published_at=datetime(2026, 8, 17, tzinfo=UTC),
        rule_count=0,
        reconciliation_status="pending",
        is_replay=False,
    )
    with pytest.raises(ValidationError):
        PolicyPublicationData.model_validate({**data.model_dump(), "signature_bytes": "AAEC"})


def _kind_of(member: str) -> str:
    return {
        "source_id": "exact_source_id",
        "folder_prefix": "folder_prefix",
        "path_glob": "path_glob",
        "extension": "extension",
        "media_type": "media_type",
        "source_type": "source_type",
        "maximum_size_bytes": "maximum_size",
    }[member]
