"""Closed SYSTEM vs DENIAL classification of the typed policy errors (G1).

The preflight boundary must distinguish "the policy denies this subject" from
"the policy system itself failed" (spec C1 of the 2026-08-24 remediation).
The split lives in exactly one place — the exclusion-policy errors module —
as two closed, disjoint code sets so a future registry code must choose a
side before any boundary can classify it. These tests pin the exact
membership, the disjointness and the predicate the boundaries call.
"""

from __future__ import annotations

from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.errors import (
    POLICY_DENIAL_ERROR_CODES,
    POLICY_SYSTEM_ERROR_CODES,
    ExclusionPolicyError,
    is_policy_system_failure,
)

_EXPECTED_SYSTEM_CODES = frozenset(
    {
        ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED,
        ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE,
    }
)

_EXPECTED_DENIAL_CODES = frozenset(
    {
        ErrorCode.EXCLUSION_POLICY_DENIED,
        ErrorCode.EXCLUSION_POLICY_INDETERMINATE,
    }
)


def test_policy_system_failure_codes_are_exactly_the_closed_system_pair() -> None:
    expected = _EXPECTED_SYSTEM_CODES
    assert expected == POLICY_SYSTEM_ERROR_CODES


def test_policy_denial_failure_codes_are_exactly_the_closed_denial_pair() -> None:
    expected = _EXPECTED_DENIAL_CODES
    assert expected == POLICY_DENIAL_ERROR_CODES


def test_policy_classification_sets_are_disjoint() -> None:
    """A registry code can never be both a system failure and a denial."""

    overlap = POLICY_SYSTEM_ERROR_CODES & POLICY_DENIAL_ERROR_CODES
    assert not overlap


def test_is_policy_system_failure_classifies_the_typed_errors() -> None:
    for system_code in _EXPECTED_SYSTEM_CODES:
        assert is_policy_system_failure(ExclusionPolicyError(system_code)) is True
    for denial_code in _EXPECTED_DENIAL_CODES:
        assert is_policy_system_failure(ExclusionPolicyError(denial_code)) is False
