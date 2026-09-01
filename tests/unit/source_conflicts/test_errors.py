"""Closed source-conflict error contract: registry pins and sentinel hygiene.

Asserts the exact closed ``source_conflict_*`` code set with fixed category,
retryability and safe-detail allowlists, the closed input-invalid reason
vocabulary, the closure of the typed exception around its codes, and that
locator, digest, token and raw-payload sentinels chained as causes never
surface in any rendering of the typed error.
"""

from __future__ import annotations

import pytest

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ERROR_DEFINITIONS, ErrorCategory, ErrorCode
from personal_os.source_conflicts.errors import (
    CONFLICT_INPUT_INVALID_REASONS,
    SourceConflictError,
)

#: The exact closed error-code set this domain adds (verbatim values).
SOURCE_CONFLICT_ERROR_CODES = {
    "source_conflict_input_invalid",
    "source_conflict_not_found",
    "source_conflict_state_invalid",
    "source_conflict_idempotency_mismatch",
    "source_conflict_evidence_unavailable",
    "source_conflict_evidence_integrity_failed",
    "source_conflict_dependency_unavailable",
    "source_conflict_commit_outcome_unknown",
}

#: Exact category, retryability and safe-detail allowlist per code.
EXPECTED_REGISTRY_CONTRACT = {
    "source_conflict_input_invalid": (ErrorCategory.VALIDATION, False, frozenset({"reason"})),
    "source_conflict_not_found": (ErrorCategory.CONFLICT, False, frozenset()),
    "source_conflict_state_invalid": (ErrorCategory.CONFLICT, False, frozenset()),
    "source_conflict_idempotency_mismatch": (ErrorCategory.CONFLICT, False, frozenset()),
    "source_conflict_evidence_unavailable": (ErrorCategory.CONFLICT, False, frozenset()),
    "source_conflict_evidence_integrity_failed": (
        ErrorCategory.INTEGRITY,
        False,
        frozenset(),
    ),
    "source_conflict_dependency_unavailable": (ErrorCategory.DEPENDENCY, True, frozenset()),
    "source_conflict_commit_outcome_unknown": (ErrorCategory.DEPENDENCY, True, frozenset()),
}

#: The codes that must accept no safe detail field at all.
NO_DETAIL_CODES = sorted(SOURCE_CONFLICT_ERROR_CODES - {"source_conflict_input_invalid"})

#: The only retryable codes: the two dependency outages.
RETRYABLE_CODES = {
    "source_conflict_dependency_unavailable",
    "source_conflict_commit_outcome_unknown",
}


def test_source_conflict_error_code_set_is_exact() -> None:
    registered = {code.value for code in SourceConflictError.allowed_codes}
    assert registered == SOURCE_CONFLICT_ERROR_CODES
    assert len(SourceConflictError.allowed_codes) == 8
    assert all(ErrorCode(value) in ERROR_DEFINITIONS for value in SOURCE_CONFLICT_ERROR_CODES)


def test_source_conflict_registry_category_retryability_and_details_are_fixed() -> None:
    assert set(EXPECTED_REGISTRY_CONTRACT) == SOURCE_CONFLICT_ERROR_CODES
    for value, (category, retryable, allowed_fields) in EXPECTED_REGISTRY_CONTRACT.items():
        definition = ERROR_DEFINITIONS[ErrorCode(value)]
        assert (definition.category, definition.is_retryable) == (category, retryable), value
        assert definition.allowed_detail_fields == allowed_fields, value
        assert definition.safe_message, value


def test_only_the_two_dependency_outages_retry() -> None:
    for value in SOURCE_CONFLICT_ERROR_CODES:
        assert ERROR_DEFINITIONS[ErrorCode(value)].is_retryable == (
            value in RETRYABLE_CODES
        ), value


def test_safe_messages_carry_no_identifiers_or_content() -> None:
    forbidden_fragments = ("uuid", "digest", "locator", "path", "token", "http", "r2")
    for value in SOURCE_CONFLICT_ERROR_CODES:
        message = ERROR_DEFINITIONS[ErrorCode(value)].safe_message.lower()
        for fragment in forbidden_fragments:
            assert fragment not in message, (value, fragment)


def test_input_invalid_reason_vocabulary_is_closed() -> None:
    assert {token.value for token in CONFLICT_INPUT_INVALID_REASONS} == {
        "conflict_kind_invalid",
        "workspace_id_invalid",
        "source_id_invalid",
        "event_id_invalid",
        "device_id_invalid",
        "idempotency_key_invalid",
        "base_version_invalid",
        "remote_version_invalid",
        "candidate_invalid",
        "locator_invalid",
        "resolution_kind_invalid",
        "resolution_event_id_invalid",
        "reviewed_remote_invalid",
        "candidate_object_invalid",
    }
    for token in CONFLICT_INPUT_INVALID_REASONS:
        assert isinstance(token, SafeToken)


def test_input_invalid_accepts_only_closed_reason_tokens() -> None:
    error = SourceConflictError(
        ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
        safe_details={"reason": CONFLICT_INPUT_INVALID_REASONS[0]},
    )
    assert error.to_safe_dict()["safe_details"] == {
        "reason": CONFLICT_INPUT_INVALID_REASONS[0].value
    }
    with pytest.raises(ValueError, match="not an accepted safe scalar"):
        SourceConflictError(
            ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
            safe_details={"reason": "locator_invalid"},
        )


def test_error_rejects_codes_outside_the_closed_set() -> None:
    with pytest.raises(ValueError, match="not valid for this exception type"):
        SourceConflictError(ErrorCode.SOURCE_NOT_FOUND)
    with pytest.raises(ValueError, match="not valid for this exception type"):
        SourceConflictError(ErrorCode.SOURCE_VERSION_CONFLICT)
    with pytest.raises(ValueError, match="not valid for this exception type"):
        SourceConflictError(ErrorCode.API_REQUEST_VALIDATION_FAILED)


@pytest.mark.parametrize("code_text", NO_DETAIL_CODES)
def test_closed_codes_accept_no_safe_detail_field(code_text: str) -> None:
    with pytest.raises(ValueError, match="not registered for this error code"):
        SourceConflictError(
            ErrorCode(code_text),
            safe_details={"reason": SafeToken.parse("candidate_invalid")},
        )
    with pytest.raises(ValueError, match="not registered for this error code"):
        SourceConflictError(
            ErrorCode(code_text),
            safe_details={"conflict_id": "00000000-0000-0000-0000-000000000000"},
        )


def test_errors_never_echo_locator_digest_token_or_payload_sentinels() -> None:
    locator_sentinel = "vault/do-not-leak/secret-notes.md"
    digest_sentinel = "ab" * 32
    token_sentinel = "Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1"
    payload_sentinel = "RAW-FILE-BYTES-DO-NOT-LEAK"
    cause = RuntimeError(
        f"locator={locator_sentinel} digest={digest_sentinel} "
        f"token={token_sentinel} payload={payload_sentinel}"
    )
    for error_code in sorted(SourceConflictError.allowed_codes, key=lambda code: code.value):
        error = SourceConflictError(error_code)
        error.__cause__ = cause
        rendered = f"{error!r} {error} {error.to_safe_dict()}"
        for sentinel in (locator_sentinel, digest_sentinel, token_sentinel, payload_sentinel):
            assert sentinel not in rendered, error_code.value


def test_registry_metadata_serializes_closed_shapes_only() -> None:
    error = SourceConflictError(
        ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
        safe_details={"reason": SafeToken.parse("candidate_object_invalid")},
    )
    assert error.to_safe_dict() == {
        "error_code": "source_conflict_input_invalid",
        "category": "validation",
        "is_retryable": False,
        "safe_message": ERROR_DEFINITIONS[
            ErrorCode.SOURCE_CONFLICT_INPUT_INVALID
        ].safe_message,
        "safe_details": {"reason": "candidate_object_invalid"},
    }
