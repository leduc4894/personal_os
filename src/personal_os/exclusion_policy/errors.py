"""Typed exclusion-policy errors and the closed safe-reason token set.

``ExclusionPolicyError`` binds the exclusion-policy subsystem to the thirteen
``exclusion_policy_*`` registry codes (spec 19). Locators, operands, titles,
paths, snapshots and subject fingerprints remain chained only as internal
causes and never enter the typed error, its safe details or diagnostics.

The reason tokens below are closed ``SafeToken`` constants: every
normalization, evaluation or draft-list validation rejection names exactly
one of them. ``reason`` plus the optional zero-based ``rule_index`` are the
only safe details ``exclusion_policy_input_invalid`` accepts.
"""

from __future__ import annotations

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError

LOCATOR_NOT_VALID_UNICODE: SafeToken = SafeToken.parse("locator_not_valid_unicode")
LOCATOR_EMPTY: SafeToken = SafeToken.parse("locator_empty")
LOCATOR_ABSOLUTE: SafeToken = SafeToken.parse("locator_absolute")
LOCATOR_TRAILING_SEPARATOR: SafeToken = SafeToken.parse("locator_trailing_separator")
LOCATOR_BACKSLASH_SEPARATOR: SafeToken = SafeToken.parse("locator_backslash_separator")
LOCATOR_SCHEME_OR_DRIVE: SafeToken = SafeToken.parse("locator_scheme_or_drive")
LOCATOR_INVALID_SEGMENT: SafeToken = SafeToken.parse("locator_invalid_segment")
LOCATOR_CONTROL_CHARACTER: SafeToken = SafeToken.parse("locator_control_character")
LOCATOR_TOO_LONG: SafeToken = SafeToken.parse("locator_too_long")
LOCATOR_TOO_MANY_SEGMENTS: SafeToken = SafeToken.parse("locator_too_many_segments")
LOCATOR_SEGMENT_TOO_LONG: SafeToken = SafeToken.parse("locator_segment_too_long")

GLOB_UNSUPPORTED_TOKEN: SafeToken = SafeToken.parse("glob_unsupported_token")
GLOB_TOO_LONG: SafeToken = SafeToken.parse("glob_too_long")
GLOB_TOO_MANY_SEGMENTS: SafeToken = SafeToken.parse("glob_too_many_segments")
GLOB_TOO_MANY_WILDCARDS: SafeToken = SafeToken.parse("glob_too_many_wildcards")

RULE_ID_INVALID: SafeToken = SafeToken.parse("rule_id_invalid")
RULE_COUNT_INVALID: SafeToken = SafeToken.parse("rule_count_invalid")
RULE_FINGERPRINT_DUPLICATE: SafeToken = SafeToken.parse("rule_fingerprint_duplicate")
OPERAND_MISSING: SafeToken = SafeToken.parse("operand_missing")
OPERAND_CONFLICT: SafeToken = SafeToken.parse("operand_conflict")
OPERAND_INVALID: SafeToken = SafeToken.parse("operand_invalid")

SUBJECT_ID_INVALID: SafeToken = SafeToken.parse("subject_id_invalid")
SUBJECT_LOCATOR_NOT_NORMALIZED: SafeToken = SafeToken.parse("subject_locator_not_normalized")
SUBJECT_FIELD_TYPE_INVALID: SafeToken = SafeToken.parse("subject_field_type_invalid")
SUBJECT_SIZE_INVALID: SafeToken = SafeToken.parse("subject_size_invalid")
SUBJECT_WORKSPACE_MISMATCH: SafeToken = SafeToken.parse("subject_workspace_mismatch")

#: Preview read orchestration reason tokens (spec 10): the bounded result-page
#: bound and the required preview subjects.
PREVIEW_LIMIT_INVALID: SafeToken = SafeToken.parse("preview_limit_invalid")

#: Closed reason tokens accepted by ``exclusion_policy_input_invalid``.
INPUT_REASONS: tuple[SafeToken, ...] = (
    LOCATOR_NOT_VALID_UNICODE,
    LOCATOR_EMPTY,
    LOCATOR_ABSOLUTE,
    LOCATOR_TRAILING_SEPARATOR,
    LOCATOR_BACKSLASH_SEPARATOR,
    LOCATOR_SCHEME_OR_DRIVE,
    LOCATOR_INVALID_SEGMENT,
    LOCATOR_CONTROL_CHARACTER,
    LOCATOR_TOO_LONG,
    LOCATOR_TOO_MANY_SEGMENTS,
    LOCATOR_SEGMENT_TOO_LONG,
    GLOB_UNSUPPORTED_TOKEN,
    GLOB_TOO_LONG,
    GLOB_TOO_MANY_SEGMENTS,
    GLOB_TOO_MANY_WILDCARDS,
    RULE_ID_INVALID,
    RULE_COUNT_INVALID,
    RULE_FINGERPRINT_DUPLICATE,
    OPERAND_MISSING,
    OPERAND_CONFLICT,
    OPERAND_INVALID,
    SUBJECT_ID_INVALID,
    SUBJECT_LOCATOR_NOT_NORMALIZED,
    SUBJECT_FIELD_TYPE_INVALID,
    SUBJECT_SIZE_INVALID,
    SUBJECT_WORKSPACE_MISMATCH,
    PREVIEW_LIMIT_INVALID,
)


class ExclusionPolicyError(ApplicationError):
    """Exclusion-policy validation, conflict, denial and dependency failures.

    The closed code set covers the thirteen ``exclusion_policy_*`` registry
    codes of spec 19. Safe detail fields are registry-bound: input validation
    accepts a single closed ``reason`` token plus the optional zero-based
    ``rule_index``; conflict and dependency codes accept only the registered
    numeric identifiers.
    """

    allowed_codes = frozenset(
        {
            ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
            ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED,
            ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT,
            ErrorCode.EXCLUSION_POLICY_PREVIEW_PENDING,
            ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED,
            ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED,
            ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE,
            ErrorCode.EXCLUSION_POLICY_CONFIRMATION_INVALID,
            ErrorCode.EXCLUSION_POLICY_DENIED,
            ErrorCode.EXCLUSION_POLICY_INDETERMINATE,
            ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED,
            ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE,
            ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN,
        }
    )


def input_invalid(reason: SafeToken, rule_index: int | None = None) -> ExclusionPolicyError:
    """Build the typed input-validation error for one closed reason token."""

    if rule_index is None:
        return ExclusionPolicyError(
            ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
            safe_details={"reason": reason},
        )
    return ExclusionPolicyError(
        ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
        safe_details={"reason": reason, "rule_index": rule_index},
    )


#: Signed-payload contract reason tokens (spec 12 and 13): every canonicalizer
#: or payload-builder rejection names exactly one of them. They are distinct
#: from the normalization/evaluation set above because the caller of the
#: signed-payload contracts is always internal composition code.
PAYLOAD_VALUE_UNSUPPORTED: SafeToken = SafeToken.parse("payload_value_unsupported")
PAYLOAD_STRING_NOT_NORMALIZED: SafeToken = SafeToken.parse("payload_string_not_normalized")
PAYLOAD_STRING_INVALID_UNICODE: SafeToken = SafeToken.parse("payload_string_invalid_unicode")
PAYLOAD_MEMBER_NAME_INVALID: SafeToken = SafeToken.parse("payload_member_name_invalid")
PAYLOAD_MEMBER_DUPLICATE: SafeToken = SafeToken.parse("payload_member_duplicate")
PAYLOAD_INTEGER_OUT_OF_RANGE: SafeToken = SafeToken.parse("payload_integer_out_of_range")
PAYLOAD_TIMESTAMP_INVALID: SafeToken = SafeToken.parse("payload_timestamp_invalid")
PAYLOAD_OVERSIZED: SafeToken = SafeToken.parse("payload_oversized")
PAYLOAD_WORKSPACE_INVALID: SafeToken = SafeToken.parse("payload_workspace_invalid")
PAYLOAD_BASE64URL_INVALID: SafeToken = SafeToken.parse("payload_base64url_invalid")
PAYLOAD_SIGNING_DOMAIN_INVALID: SafeToken = SafeToken.parse("payload_signing_domain_invalid")
PARENT_REVISION_INVALID: SafeToken = SafeToken.parse("parent_revision_invalid")
SIGNING_KEY_SIZE_INVALID: SafeToken = SafeToken.parse("signing_key_size_invalid")
SIGNING_KEY_ID_INVALID: SafeToken = SafeToken.parse("signing_key_id_invalid")
KEYSET_REVISION_INVALID: SafeToken = SafeToken.parse("keyset_revision_invalid")
KEYSET_KEY_INVALID: SafeToken = SafeToken.parse("keyset_key_invalid")
KEYSET_KEY_DUPLICATE: SafeToken = SafeToken.parse("keyset_key_duplicate")
KEYSET_CURRENT_COUNT_INVALID: SafeToken = SafeToken.parse("keyset_current_count_invalid")
KEYSET_NON_RETIRED_COUNT_INVALID: SafeToken = SafeToken.parse("keyset_non_retired_count_invalid")

#: Closed reason tokens for :class:`PolicyContractError` rejections.
PAYLOAD_CONTRACT_REASONS: tuple[SafeToken, ...] = (
    PAYLOAD_VALUE_UNSUPPORTED,
    PAYLOAD_STRING_NOT_NORMALIZED,
    PAYLOAD_STRING_INVALID_UNICODE,
    PAYLOAD_MEMBER_NAME_INVALID,
    PAYLOAD_MEMBER_DUPLICATE,
    PAYLOAD_INTEGER_OUT_OF_RANGE,
    PAYLOAD_TIMESTAMP_INVALID,
    PAYLOAD_OVERSIZED,
    PAYLOAD_WORKSPACE_INVALID,
    PAYLOAD_BASE64URL_INVALID,
    PAYLOAD_SIGNING_DOMAIN_INVALID,
    PARENT_REVISION_INVALID,
    SIGNING_KEY_SIZE_INVALID,
    SIGNING_KEY_ID_INVALID,
    KEYSET_REVISION_INVALID,
    KEYSET_KEY_INVALID,
    KEYSET_KEY_DUPLICATE,
    KEYSET_CURRENT_COUNT_INVALID,
    KEYSET_NON_RETIRED_COUNT_INVALID,
)


class PolicyContractError(ExclusionPolicyError):
    """Violation of the closed signed-payload value or encoding contracts.

    The canonicalizer, the typed payload builders and the key-identifier
    helpers raise this when internal composition code hands them a value
    outside the fixed snapshot/keyset schemas: an unsupported value type, a
    non-normalized or surrogate-bearing string, a duplicate or non-string
    member, an out-of-range integer, a naive or non-UTC timestamp, malformed
    base64url/key material, an invalid keyset chain or an oversized signed
    snapshot. The error carries exactly one closed ``reason`` token; the
    offending value, bytes and key material never enter the error or its
    diagnostics.
    """

    allowed_codes = frozenset({ErrorCode.EXCLUSION_POLICY_INPUT_INVALID})


def payload_contract_error(reason: SafeToken) -> PolicyContractError:
    """Build the typed signed-payload contract error for one closed reason."""

    return PolicyContractError(
        ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
        safe_details={"reason": reason},
    )
