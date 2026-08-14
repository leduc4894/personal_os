"""Closed source-publication error contract: codes, categories and safe details.

Asserts the exact fourteen-code registry set, the fixed category and retryability
map, the exact per-code safe-detail allowlists, disjoint closed code sets for
the two typed exception classes and rejection of arbitrary strings, raw
commands and value objects as safe details.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ERROR_DEFINITIONS, ErrorCategory, ErrorCode
from personal_os.sources.commands import IdempotencyKey, SourceTitle
from personal_os.sources.errors import (
    PROJECTION_KINDS,
    PUBLISH_INPUT_REASONS,
    RECEIPT_STALE_REASONS,
    SOURCE_STATES,
    ProjectionDispatchError,
    SourcePublicationError,
)

#: The exact closed error-code set from spec section 13 (verbatim values).
SOURCE_ERROR_CODES = {
    "source_publish_input_invalid",
    "source_not_found",
    "source_already_exists",
    "source_state_invalid",
    "source_version_conflict",
    "source_idempotency_mismatch",
    "source_event_identity_mismatch",
    "source_verified_receipt_stale",
    "source_content_object_conflict",
    "source_concurrency_busy",
    "source_concurrency_invariant_failed",
    "source_commit_outcome_unknown",
    "projection_dispatch_unavailable",
    "projection_intent_contract_invalid",
}


def test_source_error_code_registry_is_exact() -> None:
    registered = {
        code.value
        for code in (SourcePublicationError.allowed_codes | ProjectionDispatchError.allowed_codes)
    }
    assert registered == SOURCE_ERROR_CODES
    assert all(ERROR_DEFINITIONS[ErrorCode(value)] is not None for value in SOURCE_ERROR_CODES)


def test_source_error_registry_category_and_retryability_are_fixed() -> None:
    expected = {
        "source_publish_input_invalid": (ErrorCategory.VALIDATION, False),
        "source_not_found": (ErrorCategory.CONFLICT, False),
        "source_already_exists": (ErrorCategory.CONFLICT, False),
        "source_state_invalid": (ErrorCategory.CONFLICT, False),
        "source_version_conflict": (ErrorCategory.CONFLICT, False),
        "source_idempotency_mismatch": (ErrorCategory.CONFLICT, False),
        "source_event_identity_mismatch": (ErrorCategory.CONFLICT, False),
        "source_verified_receipt_stale": (ErrorCategory.VALIDATION, False),
        "source_content_object_conflict": (ErrorCategory.INTEGRITY, False),
        "source_concurrency_busy": (ErrorCategory.DEPENDENCY, True),
        "source_concurrency_invariant_failed": (ErrorCategory.INTEGRITY, False),
        "source_commit_outcome_unknown": (ErrorCategory.DEPENDENCY, True),
        "projection_dispatch_unavailable": (ErrorCategory.DEPENDENCY, True),
        "projection_intent_contract_invalid": (ErrorCategory.INTEGRITY, False),
    }
    for value, (category, retryable) in expected.items():
        definition = ERROR_DEFINITIONS[ErrorCode(value)]
        assert (definition.category, definition.is_retryable) == (category, retryable), value


@pytest.mark.parametrize(
    ("value", "allowed"),
    [
        ("source_publish_input_invalid", frozenset({"reason"})),
        ("source_not_found", frozenset({"source_id"})),
        ("source_already_exists", frozenset({"source_id"})),
        ("source_state_invalid", frozenset({"source_id", "source_state"})),
        (
            "source_version_conflict",
            frozenset({"source_id", "current_version_id", "content_version"}),
        ),
        ("source_idempotency_mismatch", frozenset({"source_id"})),
        ("source_event_identity_mismatch", frozenset({"source_id", "event_id"})),
        ("source_verified_receipt_stale", frozenset({"reason"})),
        ("source_content_object_conflict", frozenset({"source_id"})),
        ("source_concurrency_busy", frozenset({"source_id"})),
        ("source_concurrency_invariant_failed", frozenset({"source_id"})),
        ("source_commit_outcome_unknown", frozenset({"source_id"})),
        ("projection_dispatch_unavailable", frozenset({"projection_kind"})),
        ("projection_intent_contract_invalid", frozenset({"projection_kind"})),
    ],
)
def test_source_error_safe_detail_allowlists_are_exact(value: str, allowed: frozenset[str]) -> None:
    assert ERROR_DEFINITIONS[ErrorCode(value)].allowed_detail_fields == allowed


def test_publication_and_dispatch_code_sets_are_closed_and_disjoint() -> None:
    assert len(SourcePublicationError.allowed_codes) == 12
    assert len(ProjectionDispatchError.allowed_codes) == 2
    assert not SourcePublicationError.allowed_codes & ProjectionDispatchError.allowed_codes
    assert SourcePublicationError.allowed_codes <= frozenset(ErrorCode)
    assert ProjectionDispatchError.allowed_codes <= frozenset(ErrorCode)


def test_publication_error_rejects_dispatch_code_and_vice_versa() -> None:
    with pytest.raises(ValueError, match="not valid for this exception type"):
        SourcePublicationError(ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE)
    with pytest.raises(ValueError, match="not valid for this exception type"):
        ProjectionDispatchError(ErrorCode.SOURCE_NOT_FOUND)


def test_uuid_detail_fields_serialize_canonically() -> None:
    source_id = uuid4()
    current_version_id = uuid4()
    error = SourcePublicationError(
        ErrorCode.SOURCE_VERSION_CONFLICT,
        safe_details={
            "source_id": source_id,
            "current_version_id": current_version_id,
            "content_version": 3,
        },
    )
    assert error.to_safe_dict()["safe_details"] == {
        "source_id": str(source_id),
        "current_version_id": str(current_version_id),
        "content_version": 3,
    }


def test_arbitrary_strings_are_rejected_as_safe_details() -> None:
    with pytest.raises(ValueError, match="not an accepted safe scalar"):
        SourcePublicationError(ErrorCode.SOURCE_NOT_FOUND, safe_details={"source_id": "not-a-uuid"})
    with pytest.raises(ValueError, match="not an accepted safe scalar"):
        SourcePublicationError(
            ErrorCode.SOURCE_NOT_FOUND, safe_details={"source_id": ("free-text-string",)}
        )
    with pytest.raises(ValueError, match="not registered for this error code"):
        SourcePublicationError(ErrorCode.SOURCE_NOT_FOUND, safe_details={"event_id": uuid4()})


def test_value_objects_never_enter_error_details_or_rendering() -> None:
    title = SourceTitle("Do Not Embed This Title")
    key = IdempotencyKey("do-not-embed-this-key")
    with pytest.raises(ValueError, match="not an accepted safe scalar"):
        SourcePublicationError(
            ErrorCode.SOURCE_STATE_INVALID,
            safe_details={"source_id": uuid4(), "source_state": title},
        )
    error = SourcePublicationError(
        ErrorCode.SOURCE_IDEMPOTENCY_MISMATCH, safe_details={"source_id": uuid4()}
    )
    rendered = f"{error!r} {error} {error.to_safe_dict()}"
    assert "Do Not Embed This Title" not in rendered
    assert "do-not-embed-this-key" not in rendered
    assert title not in error.safe_details.values()
    assert key not in error.safe_details.values()


def test_reason_token_vocabularies_are_closed() -> None:
    assert {token.value for token in PUBLISH_INPUT_REASONS} == {
        "title_invalid",
        "idempotency_key_invalid",
        "actor_invalid",
        "expected_object_invalid",
        "client_timestamp_invalid",
    }
    assert {token.value for token in RECEIPT_STALE_REASONS} == {"older_than_allowed_age"}
    assert {token.value for token in SOURCE_STATES} == {
        "active",
        "stored_not_indexed",
        "pending",
        "deleted",
    }
    assert {token.value for token in PROJECTION_KINDS} == {"qdrant", "neo4j"}
    all_tokens = (
        *PUBLISH_INPUT_REASONS,
        *RECEIPT_STALE_REASONS,
        *SOURCE_STATES,
        *PROJECTION_KINDS,
    )
    for token in all_tokens:
        assert isinstance(token, SafeToken)


def test_reason_details_accept_only_closed_tokens() -> None:
    error = SourcePublicationError(
        ErrorCode.SOURCE_PUBLISH_INPUT_INVALID,
        safe_details={"reason": PUBLISH_INPUT_REASONS[0]},
    )
    assert error.to_safe_dict()["safe_details"] == {"reason": PUBLISH_INPUT_REASONS[0].value}
    with pytest.raises(ValueError, match="not an accepted safe scalar"):
        SourcePublicationError(
            ErrorCode.SOURCE_PUBLISH_INPUT_INVALID,
            safe_details={"reason": "title_invalid"},
        )


def test_projection_error_accepts_closed_projection_kind() -> None:
    error = ProjectionDispatchError(
        ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE,
        safe_details={"projection_kind": PROJECTION_KINDS[0]},
    )
    assert error.to_safe_dict()["safe_details"] == {"projection_kind": "qdrant"}
    assert error.is_retryable is True
